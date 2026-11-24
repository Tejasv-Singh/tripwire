"""golive:check (Day 3): payload validator, each check, and the rendered report -- offline."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

import eth_account
import pytest
from hyperliquid.utils import constants

from tripwire.config import Settings
from tripwire.golive import (
    check_agent,
    check_funds,
    check_guard,
    check_keys,
    check_payload,
    render,
    run_checks,
    validate_order_action,
)

from .test_trading import ACCOUNT, AGENT_KEY, FakeInfo

AGENT = str(eth_account.Account.from_key(AGENT_KEY).address)


def settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(_env_file=None, pairing_code="x" * 8, db_path=tmp_path / "g.db", **kw)  # type: ignore[call-arg]


def ready(tmp_path: Path, **kw: Any) -> Settings:
    return settings(tmp_path, hl_account_address=ACCOUNT, hl_agent_key=AGENT_KEY, **kw)


class Info:
    """Fake public /info: answers by request type."""

    def __init__(self, **answers: Any) -> None:
        self.answers = {"allMids": {"ETH": "1700.0"}, **answers}
        self.asked: list[str] = []

    async def __call__(self, body: dict[str, Any]) -> Any:
        self.asked.append(body["type"])
        return self.answers.get(body["type"])


def funds(perp: str = "0", spot: str = "0") -> dict[str, Any]:
    return {
        "clearinghouseState": {"marginSummary": {"accountValue": perp}},
        "spotClearinghouseState": {"balances": [{"coin": "USDC", "total": spot}]},
    }


AGENT_OK = {"userRole": {"role": "agent", "data": {"user": ACCOUNT}}}


# --- payload validator -----------------------------------------------------------------------


async def good_action(tmp_path: Path) -> dict[str, Any]:
    result, _ = await check_payload(ready(tmp_path), Info(), info_factory=FakeInfo)
    assert result.status == "PASS", result.detail
    return {
        "type": "order",
        "grouping": "normalTpsl",
        "orders": [
            {"a": 4, "b": True, "p": "1785", "s": "0.0088", "r": False,
             "t": {"limit": {"tif": "Ioc"}}},
            {"a": 4, "b": False, "p": "1453.5", "s": "0.0088", "r": True,
             "t": {"trigger": {"isMarket": True, "triggerPx": "1615", "tpsl": "sl"}}},
        ],
    }  # fmt: skip


async def test_validator_accepts_a_canonical_payload(tmp_path: Path) -> None:
    assert validate_order_action(await good_action(tmp_path), {4: 4}, ref_px=1700.0) == []


@pytest.mark.parametrize(
    ("path", "value", "problem"),
    [
        ((0, "p"), "1785.123", "breaks the tick rule"),
        ((0, "s"), "0.00881", "not a multiple of the lot"),
        ((1, "r"), False, "must be reduce-only"),
        ((1, "t"), {"trigger": {"isMarket": True, "triggerPx": "1615.04", "tpsl": "sl"}},
         "triggerPx '1615.04' breaks the tick rule"),
        ((0, "t"), {"limit": {"tif": "Day"}}, "tif 'Day'"),
        ((0, "a"), 99, "unknown asset index 99"),
        ((0, "s"), "0.0001", "below Hyperliquid's $10 minimum"),
        ((0, "p"), 1785.0, "breaks the tick rule"),  # numbers must be wire strings
    ],
)  # fmt: skip
async def test_validator_names_each_problem(
    tmp_path: Path, path: tuple[int, str], value: Any, problem: str
) -> None:
    action = copy.deepcopy(await good_action(tmp_path))
    action["orders"][path[0]][path[1]] = value
    problems = validate_order_action(action, {4: 4}, ref_px=1700.0)
    assert any(problem in p for p in problems), problems


async def test_validator_checks_grouping_and_type(tmp_path: Path) -> None:
    action = await good_action(tmp_path)
    assert validate_order_action({**action, "grouping": "oco"}, {4: 4})[0].startswith("grouping")
    assert validate_order_action({"type": "cancel"}, {4: 4}) == [
        "action type is 'cancel', not 'order'"
    ]


# --- individual checks ---------------------------------------------------------------------------


def test_keys(tmp_path: Path) -> None:
    r = check_keys(settings(tmp_path))
    assert (r.status, r.detail) == ("FAIL", "HL_ACCOUNT_ADDRESS is empty; HL_AGENT_KEY is empty")
    assert check_keys(ready(tmp_path)).status == "PASS"


async def test_agent_outcomes(tmp_path: Path) -> None:
    s = ready(tmp_path)
    assert (await check_agent(settings(tmp_path), "", Info())).status == "SKIP"
    assert (await check_agent(s, AGENT, Info(**AGENT_OK))).status == "PASS"
    other = Info(userRole={"role": "agent", "data": {"user": "0x" + "9" * 40}})
    assert "not of" in (await check_agent(s, AGENT, other)).detail
    future, past = int(time.time() * 1000) + 10**7, int(time.time() * 1000) - 10**7
    named = Info(
        userRole={"role": "missing"},
        extraAgents=[{"name": "tw", "address": AGENT, "validUntil": future}],
    )
    assert (await check_agent(s, AGENT, named)).status == "PASS"
    expired = Info(
        userRole={"role": "missing"},
        extraAgents=[{"name": "tw", "address": AGENT, "validUntil": past}],
    )
    assert (await check_agent(s, AGENT, expired)).detail == "agent 'tw' has expired"
    unknown = Info(userRole={"role": "missing"}, extraAgents=[])
    r = await check_agent(s, AGENT, unknown)
    assert r.status == "FAIL" and "role: missing" in r.detail and "authorize" in r.fix


async def test_funds_outcomes(tmp_path: Path) -> None:
    assert (await check_funds(settings(tmp_path), Info())).status == "SKIP"
    low = await check_funds(ready(tmp_path), Info(**funds("5", "3")))
    assert (low.status, low.detail) == ("FAIL", "$8.00 (perps $5.00 + spot $3.00)")
    spot_only = await check_funds(ready(tmp_path), Info(**funds("0", "1000")))
    assert spot_only.status == "PASS" and "transfer some to perps" in spot_only.detail
    assert (await check_funds(ready(tmp_path), Info(**funds("25", "0")))).status == "PASS"


def test_guard(tmp_path: Path) -> None:
    assert check_guard(settings(tmp_path)).status == "PASS"
    bad = check_guard(settings(tmp_path, hl_api_url=constants.MAINNET_API_URL))
    assert bad.status == "FAIL" and "not testnet" in bad.detail


async def test_payload_signs_with_the_agent_key_when_present(tmp_path: Path) -> None:
    result, signer = await check_payload(ready(tmp_path), Info(), info_factory=FakeInfo)
    assert result.status == "PASS" and signer == AGENT and "agent key" in result.detail
    result, signer = await check_payload(settings(tmp_path), Info(), info_factory=FakeInfo)
    assert result.status == "PASS" and "throwaway key" in result.detail and signer != AGENT


# --- the whole report ----------------------------------------------------------------------------


async def test_nothing_configured_gives_one_clean_fail(tmp_path: Path) -> None:
    s = settings(tmp_path)
    info = Info()
    results = await run_checks(s, info, info_factory=FakeInfo)
    assert [r.status for r in results] == ["FAIL", "SKIP", "SKIP", "PASS", "PASS"]
    assert info.asked == ["allMids"]  # nothing account-specific was even asked
    text = render(results, s)
    assert "Result: FAIL - 1 failed, 2 skipped, 2 passed.  DRY_RUN in .env: true" in text
    assert (
        "Failed: Account + agent key in .env (HL_ACCOUNT_ADDRESS is empty; HL_AGENT_KEY is empty)"
        in text
    )
    assert "Then run:  npm run golive:check" in text


async def test_everything_ready_says_what_to_do_next(tmp_path: Path) -> None:
    s = ready(tmp_path)
    results = await run_checks(s, Info(**AGENT_OK, **funds("1000")), info_factory=FakeInfo)
    assert all(r.status == "PASS" for r in results), [(r.title, r.detail) for r in results]
    assert "do docs/GO_LIVE.md step 3" in render(results, s)
    live = ready(tmp_path, dry_run=False)
    assert "from step 4" in render(results, live)


async def test_funds_missing_is_the_only_fail_once_keys_work(tmp_path: Path) -> None:
    s = ready(tmp_path)
    results = await run_checks(s, Info(**AGENT_OK, **funds("0", "0")), info_factory=FakeInfo)
    assert [r.key for r in results if r.status == "FAIL"] == ["funds"]
    assert "faucet" in render(results, s)
