"""Go-live readiness checks (`npm run golive:check`, scripts/golive_check.py).

In order -- a check that needs something an earlier one found missing is SKIPped, not FAILed,
so the FAIL list names only real problems:
  1. keys      HL_ACCOUNT_ADDRESS and HL_AGENT_KEY present (valid format)
  2. agent     the agent wallet is authorized for that account (/info userRole, extraAgents)
  3. funds     testnet USDC (perps account value + spot USDC) > $20
  4. guard     HL_API_URL is the testnet URL and assert_testnet() refuses mainnet
  5. payload   with DRY_RUN forced on: build and sign a $15 ETH market buy with a stop-loss
               through the real adapter, then validate it -- tick and lot rules per asset,
               trigger legs reduce-only, the SDK re-encodes it byte-identically
               (order_request_to_order_wire), and the signature recovers to the signer for
               TESTNET. Nothing is sent.
Only public /info endpoints are called; the agent key never leaves this process.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from hyperliquid.utils import constants

from tripwire.config import TESTNET_API_URL, Settings, TestnetGuardError, assert_testnet
from tripwire.hl.positions import to_float
from tripwire.trading import is_valid_px, is_valid_sz

Status = Literal["PASS", "FAIL", "SKIP"]
InfoPost = Callable[[dict[str, Any]], Awaitable[Any]]  # POST /info (testnet)

MIN_BALANCE_USD = 20.0
TEST_NOTIONAL_USD = 15.0
MIN_ORDER_USD = 10.0  # Hyperliquid's minimum order value
GROUPINGS = {"na", "normalTpsl", "positionTpsl"}
TIFS = {"Gtc", "Ioc", "Alo"}


@dataclass(frozen=True, slots=True)
class CheckResult:
    key: str
    title: str
    status: Status
    detail: str
    fix: str = ""  # what to do if it failed


def short(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if addr else ""


# --- payload validation (pure) ---------------------------------------------------------------


def _decode_order_type(t: Mapping[str, Any]) -> dict[str, Any]:
    if "limit" in t:
        return {"limit": dict(t["limit"])}
    trig = t["trigger"]
    return {
        "trigger": {
            "isMarket": trig["isMarket"],
            "triggerPx": float(trig["triggerPx"]),
            "tpsl": trig["tpsl"],
        }
    }


def validate_order_action(
    action: Mapping[str, Any], sz_decimals: Mapping[int, int], ref_px: float | None = None
) -> list[str]:
    """Problems with an /exchange "order" action ([] = valid). `sz_decimals` maps asset index ->
    szDecimals; `ref_px` (the current price) enables the $10 minimum check on the entry leg."""
    from hyperliquid.utils.signing import order_request_to_order_wire

    problems: list[str] = []
    if action.get("type") != "order":
        return [f"action type is {action.get('type')!r}, not 'order'"]
    if action.get("grouping") not in GROUPINGS:
        problems.append(f"grouping {action.get('grouping')!r} is not one of {sorted(GROUPINGS)}")
    orders = action.get("orders")
    if not isinstance(orders, list) or not orders:
        return problems + ["no orders"]
    for i, w in enumerate(orders):
        tag = f"order {i}"
        if not isinstance(w, Mapping) or not {"a", "b", "p", "s", "r", "t"} <= set(w):
            problems.append(f"{tag}: missing keys (needs a, b, p, s, r, t)")
            continue
        asset, szd = w["a"], sz_decimals.get(w["a"]) if isinstance(w["a"], int) else None
        if szd is None:
            problems.append(f"{tag}: unknown asset index {asset!r}")
            continue
        if not isinstance(w["b"], bool) or not isinstance(w["r"], bool):
            problems.append(f"{tag}: b and r must be booleans")
        px, sz = to_float(w["p"]), to_float(w["s"])
        if not isinstance(w["p"], str) or px is None or not is_valid_px(px, szd):
            problems.append(f"{tag}: price {w['p']!r} breaks the tick rule (szDecimals {szd})")
        if not isinstance(w["s"], str) or sz is None or not is_valid_sz(sz, szd):
            problems.append(
                f"{tag}: size {w['s']!r} is not a multiple of the lot (szDecimals {szd})"
            )
        t = w["t"]
        if isinstance(t, Mapping) and "limit" in t:
            if t["limit"].get("tif") not in TIFS:
                problems.append(f"{tag}: tif {t['limit'].get('tif')!r} not in {sorted(TIFS)}")
        elif isinstance(t, Mapping) and "trigger" in t:
            trig = t["trigger"]
            tpx = to_float(trig.get("triggerPx"))
            if trig.get("tpsl") not in ("sl", "tp") or not isinstance(trig.get("isMarket"), bool):
                problems.append(f"{tag}: trigger needs tpsl sl/tp and a boolean isMarket")
            if tpx is None or not is_valid_px(tpx, szd):
                problems.append(f"{tag}: triggerPx {trig.get('triggerPx')!r} breaks the tick rule")
            if w["r"] is not True:
                problems.append(f"{tag}: a stop-loss / take-profit leg must be reduce-only")
        else:
            problems.append(f"{tag}: t must be a limit or a trigger order type")
            continue
        if any(p.startswith(tag) for p in problems) or px is None or sz is None:
            continue
        request = {
            "coin": "",
            "is_buy": w["b"],
            "sz": sz,
            "limit_px": px,
            "order_type": _decode_order_type(t),
            "reduce_only": w["r"],
        }
        if order_request_to_order_wire(request, asset) != {k: w[k] for k in "abprst"}:
            problems.append(f"{tag}: the SDK re-encodes it differently (wire not canonical)")
        if i == 0 and not w["r"] and ref_px and sz * ref_px < MIN_ORDER_USD:
            problems.append(f"{tag}: ${sz * ref_px:.2f} is below Hyperliquid's $10 minimum")
    return problems


def signature_signer(payload: Mapping[str, Any], *, mainnet: bool = False) -> str:
    """Address that signed this /exchange payload (recovered as the SDK would verify it)."""
    from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

    return str(
        recover_agent_or_user_from_l1_action(
            payload["action"],
            payload["signature"],
            payload.get("vaultAddress"),
            payload["nonce"],
            payload.get("expiresAfter"),
            mainnet,
        )
    )


# --- the five checks ---------------------------------------------------------------------------


def check_keys(settings: Settings) -> CheckResult:
    missing = []
    if not settings.hl_account_address:
        missing.append("HL_ACCOUNT_ADDRESS is empty")
    if not settings.hl_agent_key.get_secret_value():
        missing.append("HL_AGENT_KEY is empty")
    if missing:
        return CheckResult(
            "keys", "Account + agent key in .env", "FAIL", "; ".join(missing),
            "Put your MAIN testnet address in HL_ACCOUNT_ADDRESS and the API wallet's private key "
            "in HL_AGENT_KEY (.env). See docs/GO_LIVE.md step 1.",
        )  # fmt: skip
    return CheckResult(
        "keys", "Account + agent key in .env", "PASS",
        f"account {short(settings.hl_account_address)}, agent key set (format OK)",
    )  # fmt: skip


async def check_agent(settings: Settings, agent_address: str, post: InfoPost) -> CheckResult:
    title = "Agent authorized for the account"
    account = settings.hl_account_address
    if not account or not agent_address:
        return CheckResult("agent", title, "SKIP", "needs both values from check 1")
    role = await post({"type": "userRole", "user": agent_address})
    if isinstance(role, Mapping) and role.get("role") == "agent":
        owner = str((role.get("data") or {}).get("user", "")).lower()
        if owner == account.lower():
            return CheckResult(
                "agent", title, "PASS", f"{short(agent_address)} is an agent of {short(account)}"
            )
        return CheckResult(
            "agent", title, "FAIL",
            f"{short(agent_address)} is an agent of {short(owner)}, not of {short(account)}",
            "HL_ACCOUNT_ADDRESS must be the account that authorized this API wallet.",
        )  # fmt: skip
    extra = await post({"type": "extraAgents", "user": account})
    now_ms = int(time.time() * 1000)
    for a in extra if isinstance(extra, list) else []:
        if str(a.get("address", "")).lower() == agent_address.lower():
            if int(a.get("validUntil") or 0) > now_ms:
                return CheckResult("agent", title, "PASS", f"named agent {a.get('name')!r}")
            return CheckResult(
                "agent", title, "FAIL", f"agent {a.get('name')!r} has expired",
                "Generate and authorize a new API wallet, put its key in HL_AGENT_KEY.",
            )  # fmt: skip
    got = role.get("role") if isinstance(role, Mapping) else role
    return CheckResult(
        "agent", title, "FAIL",
        f"{short(agent_address)} is not an agent of this account (role: {got})",
        "On app.hyperliquid-testnet.xyz -> API, authorize this API wallet for your account.",
    )  # fmt: skip


async def check_funds(settings: Settings, post: InfoPost) -> CheckResult:
    title = f"Testnet USDC > ${MIN_BALANCE_USD:.0f}"
    account = settings.hl_account_address
    if not account:
        return CheckResult("funds", title, "SKIP", "needs HL_ACCOUNT_ADDRESS (funds not checked)")
    perps = await post({"type": "clearinghouseState", "user": account})
    spot = await post({"type": "spotClearinghouseState", "user": account})
    perp_value = to_float(((perps or {}).get("marginSummary") or {}).get("accountValue")) or 0.0
    spot_usdc = sum(
        to_float(b.get("total")) or 0.0
        for b in (spot or {}).get("balances", [])
        if b.get("coin") == "USDC"
    )
    total = perp_value + spot_usdc
    detail = f"${total:,.2f} (perps ${perp_value:,.2f} + spot ${spot_usdc:,.2f})"
    if total <= MIN_BALANCE_USD:
        return CheckResult(
            "funds", title, "FAIL", detail,
            "Claim testnet USDC from the faucet on app.hyperliquid-testnet.xyz.",
        )  # fmt: skip
    hint = "" if perp_value > MIN_BALANCE_USD else " - most is in spot: transfer some to perps"
    return CheckResult("funds", title, "PASS", detail + hint)


def check_guard(settings: Settings) -> CheckResult:
    title = "Testnet URL guard"
    try:
        assert_testnet(settings.hl_api_url)
    except TestnetGuardError:
        return CheckResult(
            "guard", title, "FAIL", f"HL_API_URL={settings.hl_api_url!r} is not testnet",
            f"Set HL_API_URL={TESTNET_API_URL} (the backend refuses anything else anyway).",
        )  # fmt: skip
    try:
        assert_testnet(constants.MAINNET_API_URL)
    except TestnetGuardError:
        return CheckResult(
            "guard", title, "PASS", "HL_API_URL is the testnet URL; mainnet is refused"
        )
    return CheckResult(
        "guard", title, "FAIL", "assert_testnet accepted the MAINNET URL", "Do not go live."
    )


async def check_payload(
    settings: Settings,
    post: InfoPost,
    *,
    info_factory: Callable[[], Any] | None = None,
) -> tuple[CheckResult, str]:
    """Returns (result, signer address used). Signs with HL_AGENT_KEY if set, else throwaway."""
    from tripwire.exchanges.hyperliquid import HyperliquidAdapter
    from tripwire.hl.feed import apply_ws_message
    from tripwire.orders import OpenOrder
    from tripwire.state import MarketState

    title = f"${TEST_NOTIONAL_USD:.0f} order signs and validates (DRY RUN)"
    mids = await post({"type": "allMids"})
    eth = to_float((mids or {}).get("ETH"))
    if eth is None:
        return CheckResult(
            "payload", title, "FAIL", "no ETH price from /info allMids", "Retry."
        ), ""
    state = MarketState()
    apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": str(eth)}}})
    adapter = HyperliquidAdapter(
        state,
        base_url=settings.hl_api_url,
        account_address=settings.hl_account_address,
        agent_key=settings.hl_agent_key.get_secret_value(),
        dry_run=True,  # forced: this check never sends anything
        info_factory=info_factory,
    )
    order = OpenOrder("ETH", "LONG", TEST_NOTIONAL_USD, 1.0, None, round(eth * 0.95, 1), None)
    result = await adapter.place_order(order)
    if result.status != "dry_run":
        detail = f"adapter answered {result.status}: {result.message}"
        return CheckResult(
            "payload", title, "FAIL", detail, "See the backend log."
        ), adapter.agent_address
    records = list(adapter.dry_run_records)
    info = adapter._info
    sz_decimals = {info.name_to_asset("ETH"): adapter.size_decimals("ETH") or 0}
    problems: list[str] = []
    order_records = [r for r in records if r["payload"]["action"].get("type") == "order"]
    if len(order_records) != 1:
        problems.append(f"expected 1 order action, got {len(order_records)}")
    for rec in order_records:
        problems += validate_order_action(rec["payload"]["action"], sz_decimals, ref_px=eth)
    for rec in records:
        if signature_signer(rec["payload"]).lower() != adapter.agent_address.lower():
            problems.append(f"{rec['what']}: signature does not recover to the signer on testnet")
    if problems:
        return (
            CheckResult(
                "payload",
                title,
                "FAIL",
                "; ".join(problems)[:300],
                "Fix the payload before going live.",
            ),
            adapter.agent_address,
        )
    legs = order_records[0]["payload"]["action"]["orders"]
    entry, stop = legs[0], legs[1]
    signer = f"{adapter.signer_kind} key {short(adapter.agent_address)}"
    detail = (
        f"buy {entry['s']} ETH IOC limit {entry['p']} (~${float(entry['s']) * eth:.2f}) + stop "
        f"{stop['t']['trigger']['triggerPx']} (limit {stop['p']}); valid ticks/lots, "
        f"SDK-identical wire, signature recovers on testnet ({signer})"
    )
    return CheckResult("payload", title, "PASS", detail), adapter.agent_address


def agent_address_of(settings: Settings) -> str:
    key = settings.hl_agent_key.get_secret_value()
    if not key:
        return ""
    import eth_account

    return str(eth_account.Account.from_key(key).address)


async def run_checks(
    settings: Settings, post: InfoPost, *, info_factory: Callable[[], Any] | None = None
) -> list[CheckResult]:
    results = [check_keys(settings)]
    agent = agent_address_of(settings) if results[0].status == "PASS" else ""
    results.append(await check_agent(settings, agent, post))
    results.append(await check_funds(settings, post))
    results.append(check_guard(settings))
    payload, _ = await check_payload(settings, post, info_factory=info_factory)
    results.append(payload)
    return results


def next_command(results: list[CheckResult], settings: Settings) -> str:
    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        return f"{failed[0].fix}\n      Then run:  npm run golive:check"
    if settings.dry_run:
        return (
            "All checks passed. Restart the backend (npm run backend:dev), do docs/GO_LIVE.md "
            "step 3 (a DRY RUN close on the glasses), then set DRY_RUN=false in .env and restart."
        )
    return (
        "All checks passed and DRY_RUN=false: follow docs/GO_LIVE.md from step 4 "
        "(first live close)."
    )


def render(results: list[CheckResult], settings: Settings) -> str:
    lines = ["Tripwire go-live check (Hyperliquid TESTNET only)", ""]
    for i, r in enumerate(results, 1):
        lines.append(f" {i}. {r.status:<4}  {r.title}")
        lines.append(f"          {r.detail}")
    failed = [r for r in results if r.status == "FAIL"]
    skipped = [r for r in results if r.status == "SKIP"]
    lines.append("")
    lines.append(
        f"Result: {'FAIL' if failed else 'PASS'} - {len(failed)} failed, {len(skipped)} skipped, "
        f"{len(results) - len(failed) - len(skipped)} passed.  DRY_RUN in .env: "
        f"{'true' if settings.dry_run else 'false'}"
    )
    if failed:
        lines.append("Failed: " + "; ".join(f"{r.title} ({r.detail})" for r in failed))
    lines.append(f"Next: {next_command(results, settings)}")
    return "\n".join(lines)
