"""Pure order helpers shared by the exchange adapters (unit-tested in tests/test_trading.py).

Day 2 kept a `HyperliquidExecutor` here; since Day 2 (continued) order execution lives in
`tripwire/exchanges/` behind the `ExchangeAdapter` interface. What stays here has no I/O:
rounding to the exchange's lot / tick rules and turning `/exchange` replies into OrderResults.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal
from typing import Any

from tripwire.exchanges.base import TradingStatus
from tripwire.hl.positions import to_float
from tripwire.orders import CloseOrder, OrderResult, ResultStatus

SLIPPAGE = 0.05  # IOC limit = mid * (1 ± 5 %), the SDK's DEFAULT_SLIPPAGE
HTTP_TIMEOUT_S = 10.0  # per HTTP request to Hyperliquid
ORDER_TIMEOUT_S = 25.0  # one whole order call (sign + post)
MAX_MESSAGE_LEN = 120

CloseRequest = CloseOrder  # Day-2 name
__all__ = ["CloseRequest", "OrderResult", "ResultStatus", "TradingStatus"]

# What the Hyperliquid adapter "receives" in DRY_RUN instead of a real /exchange reply.
DRY_RUN_REPLY: dict[str, Any] = {
    "status": "ok",
    "dry_run": True,
    "response": {"type": "dryRun", "data": {"statuses": ["dryRun"]}},
}


def round_size_down(size: float, decimals: int) -> float:
    """Round DOWN to the coin's szDecimals so a partial close never exceeds what was shown."""
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(size)).quantize(quantum, rounding=ROUND_DOWN))


PERP_MAX_DECIMALS = 6  # prices: <= 5 significant figures and <= 6 - szDecimals decimals
# Limit price of a MARKET trigger (stop-loss / take-profit) = trigger -+ this. The SDK's
# examples/basic_tpsl.py puts a sell stop's limit below its trigger (2500 vs 2600) for the same
# reason: if price gaps through the trigger, a limit AT the trigger cannot fill and the stop
# fails exactly when it is needed. 10 % matches the Hyperliquid UI's TP/SL market slippage.
TRIGGER_SLIPPAGE = 0.10


def round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid perp price rules (SDK examples/rounding.py): above 100,000 any integer is
    valid; otherwise at most 5 significant figures and at most (6 - szDecimals) decimals."""
    if px > 100_000:
        return float(round(px))
    rounded = round(float(f"{px:.5g}"), max(0, PERP_MAX_DECIMALS - sz_decimals))
    if rounded <= 0:  # below the smallest tick: never sign a zero price
        raise ValueError(f"price {px!r} is below the smallest tick for szDecimals={sz_decimals}")
    return rounded


def is_valid_px(px: float, sz_decimals: int) -> bool:
    """True if the exchange accepts this perp price as-is (no rounding needed)."""
    try:
        return px > 0 and round_px(px, sz_decimals) == px
    except ValueError:
        return False


def is_valid_sz(sz: float, sz_decimals: int) -> bool:
    """True if `sz` is a positive multiple of the coin's lot (10 ** -szDecimals)."""
    return sz > 0 and round_size_down(sz, sz_decimals) == sz


def trigger_limit_px(trigger_px: float, is_buy: bool, sz_decimals: int) -> float:
    """Worst acceptable fill price for a market trigger order: a buy may pay up to +10 %,
    a sell may receive down to -10 %, rounded to a valid tick."""
    raw = trigger_px * (1 + TRIGGER_SLIPPAGE) if is_buy else trigger_px * (1 - TRIGGER_SLIPPAGE)
    return round_px(raw, sz_decimals)


def short_message(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE_LEN else text[: MAX_MESSAGE_LEN - 3] + "..."


def parse_order_response(resp: Any) -> OrderResult:
    """Turn an `/exchange` order reply into an OrderResult (first order's status wins).

    ok:     {"status":"ok","response":{"type":"order","data":{"statuses":[...]}}}
            statuses[i]: {"filled":{"totalSz","avgPx","oid"}} | {"resting":{"oid"}} |
                         {"error":"..."} | "waitingForTrigger" | "waitingForFill" | "success"
    not ok: {"status":"err","response":"<message>"}
    None:   nothing was sent (no position for that coin).
    """
    if resp is None:
        return OrderResult(False, "rejected", "No open position for that coin on the exchange")
    if not isinstance(resp, Mapping):
        return OrderResult(False, "error", short_message(f"Unexpected reply: {resp!r}"))
    if resp.get("status") != "ok":
        detail = resp.get("response") or resp.get("error") or resp
        return OrderResult(False, "rejected", short_message(str(detail)))
    response = resp.get("response")
    data = response.get("data") if isinstance(response, Mapping) else None
    statuses = data.get("statuses") if isinstance(data, Mapping) else None
    if not isinstance(statuses, list) or not statuses:
        return OrderResult(False, "error", "Empty order reply from Hyperliquid")
    st = statuses[0]
    if isinstance(st, str):  # trigger orders / TP-SL children and cancels answer with a word
        if st in ("waitingForTrigger", "waitingForFill", "success"):
            return OrderResult(True, "placed", f"Placed ({st})")
        if st == "dryRun":
            return OrderResult(True, "dry_run", "DRY RUN: signed, not sent")
        return OrderResult(False, "error", short_message(f"Unknown order status: {st!r}"))
    if not isinstance(st, Mapping):
        return OrderResult(False, "error", "Empty order reply from Hyperliquid")
    if isinstance(st.get("filled"), Mapping):
        f = st["filled"]
        sz, px = to_float(f.get("totalSz")), to_float(f.get("avgPx"))
        oid = f.get("oid") if isinstance(f.get("oid"), int) else None
        return OrderResult(
            True, "filled", f"Filled {f.get('totalSz')} @ {f.get('avgPx')}", sz, px, oid
        )
    if isinstance(st.get("resting"), Mapping):
        oid = st["resting"].get("oid")
        oid = oid if isinstance(oid, int) else None
        return OrderResult(True, "resting", f"Order resting (oid {oid})", oid=oid)
    if "error" in st:
        return OrderResult(False, "rejected", short_message(str(st["error"])))
    return OrderResult(False, "error", short_message(f"Unknown order status: {dict(st)!r}"))
