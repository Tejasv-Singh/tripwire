"""Order executor: testnet guard, size rounding, reply parsing — fake Exchange, no network."""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from hyperliquid.utils import constants

from tripwire.config import TESTNET_API_URL, TestnetGuardError
from tripwire.trading import (
    SLIPPAGE,
    CloseRequest,
    DisabledExecutor,
    HyperliquidExecutor,
    parse_order_response,
    round_size_down,
)

ACCOUNT = "0xabcdef0123456789abcdef0123456789abcdef01"
FILLED = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {"statuses": [{"filled": {"totalSz": "0.25", "avgPx": "3499.1", "oid": 77}}]},
    },
}


class FakeInfo:
    asset_to_sz_decimals = {4: 4}

    def name_to_asset(self, coin: str) -> int:
        return {"ETH": 4}[coin]


class FakeExchange:
    def __init__(self, reply: Any = FILLED, base_url: str = TESTNET_API_URL) -> None:
        self.base_url = base_url
        self.info = FakeInfo()
        self.reply = reply
        self.calls: list[tuple[Any, ...]] = []

    def market_close(
        self, coin: str, sz: float | None = None, px: float | None = None, slippage: float = 0.05
    ) -> Any:
        self.calls.append((coin, sz, px, slippage))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def executor_with(fake: FakeExchange) -> HyperliquidExecutor:
    return HyperliquidExecutor(
        base_url=TESTNET_API_URL,
        account_address=ACCOUNT,
        agent_key="",
        exchange_factory=lambda: fake,
    )


def test_round_size_down() -> None:
    assert round_size_down(0.125, 4) == 0.125
    assert round_size_down(0.125, 2) == 0.12  # down, not half-even / half-up
    assert round_size_down(0.19999, 1) == 0.1
    assert round_size_down(10.0, 0) == 10.0
    assert round_size_down(0.1 + 0.2, 2) == 0.3  # float noise doesn't push it below


@pytest.mark.parametrize(
    ("resp", "ok", "status", "message"),
    [
        (FILLED, True, "filled", "Filled 0.25 @ 3499.1"),
        (
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 5}}]}}},
            True, "resting", "Order resting (oid 5)",
        ),
        (
            {"status": "ok", "response": {"data": {"statuses": [
                {"error": "Order could not immediately match against any resting orders."}
            ]}}},
            False, "rejected", "Order could not immediately match against any resting orders.",
        ),
        ({"status": "err", "response": "User or API Wallet does not exist."},
         False, "rejected", "User or API Wallet does not exist."),
        (None, False, "rejected", "No open position for that coin on the exchange"),
        ({"status": "ok", "response": {"data": {"statuses": []}}},
         False, "error", "Empty order reply from Hyperliquid"),
        ("garbage", False, "error", "Unexpected reply: 'garbage'"),
    ],
)  # fmt: skip
def test_parse_order_response(resp: Any, ok: bool, status: str, message: str) -> None:
    r = parse_order_response(resp)
    assert (r.ok, r.status, r.message) == (ok, status, message)


def test_parse_filled_extracts_numbers() -> None:
    r = parse_order_response(FILLED)
    assert (r.filled_size, r.avg_px, r.oid) == (0.25, 3499.1, 77)


def test_long_error_messages_are_shortened() -> None:
    r = parse_order_response({"status": "err", "response": "x" * 500})
    assert len(r.message) == 120 and r.message.endswith("...")


def test_executor_refuses_mainnet() -> None:
    with pytest.raises(TestnetGuardError):
        HyperliquidExecutor(
            base_url=constants.MAINNET_API_URL, account_address=ACCOUNT, agent_key="0x" + "1" * 64
        )


async def test_executor_refuses_an_exchange_not_on_testnet() -> None:
    ex = executor_with(FakeExchange(base_url=constants.MAINNET_API_URL))
    r = await ex.close(CloseRequest("ETH", "LONG", 1.0, None))
    assert not r.ok and r.message == "Refused: exchange is not testnet"


async def test_close_all_passes_none_and_slippage() -> None:
    fake = FakeExchange()
    ex = executor_with(fake)
    assert ex.size_decimals("ETH") is None  # not warmed up yet
    r = await ex.close(CloseRequest("ETH", "LONG", 1.0, None))
    assert r.ok and r.status == "filled"
    assert fake.calls == [("ETH", None, None, SLIPPAGE)]
    assert ex.status == "ready"


async def test_partial_close_rounds_to_sz_decimals() -> None:
    fake = FakeExchange()
    ex = executor_with(fake)
    await ex.warm_up()
    assert ex.size_decimals("ETH") == 4 and ex.size_decimals("NOPE") is None
    await ex.close(CloseRequest("ETH", "LONG", 0.25, 0.123456))
    assert fake.calls[-1][1] == 0.1234


async def test_partial_close_that_rounds_to_zero_is_not_sent() -> None:
    fake = FakeExchange()
    ex = executor_with(fake)
    r = await ex.close(CloseRequest("ETH", "LONG", 0.25, 0.00004))
    assert not r.ok and fake.calls == []


async def test_sdk_exceptions_become_error_results() -> None:
    class ClientErrorLike(Exception):
        error_message = "Insufficient margin"

    ex = executor_with(FakeExchange(reply=ClientErrorLike()))
    r = await ex.close(CloseRequest("ETH", "LONG", 1.0, None))
    assert (r.ok, r.status, r.message) == (False, "error", "Insufficient margin")


async def test_warm_up_failure_is_reported_not_raised() -> None:
    def boom() -> Any:
        raise ConnectionError("no route to host")

    ex = HyperliquidExecutor(
        base_url=TESTNET_API_URL, account_address=ACCOUNT, agent_key="", exchange_factory=boom
    )
    await ex.warm_up()
    assert ex.status == "error" and "no route to host" in ex.detail
    r = await ex.close(CloseRequest("ETH", "LONG", 1.0, None))
    assert not r.ok and "unreachable" in r.message


def test_real_executor_keeps_only_the_public_agent_address() -> None:
    key = "0x" + secrets.token_hex(32)
    ex = HyperliquidExecutor(base_url=TESTNET_API_URL, account_address=ACCOUNT, agent_key=key)
    assert ex.agent_address.startswith("0x") and len(ex.agent_address) == 42
    assert all(key not in repr(v) for v in vars(ex).values())  # nothing on self holds the key


async def test_disabled_executor_never_trades() -> None:
    ex = DisabledExecutor()
    assert ex.status == "off" and ex.size_decimals("ETH") is None
    r = await ex.close(CloseRequest("ETH", "LONG", 1.0, None))
    assert not r.ok
