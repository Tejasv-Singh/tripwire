"""Exchange adapters: Hyperliquid (fake Info + captured POSTs, no network), fake, Bybit stub,
plus the pure helpers (rounding, reply parsing)."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

import pytest
from hyperliquid.utils import constants

import tripwire.exchanges.hyperliquid as hl_mod
from tripwire.config import TESTNET_API_URL, TestnetGuardError
from tripwire.exchanges.base import ExchangeAdapter, StopInfo
from tripwire.exchanges.bybit_demo import NOT_IMPLEMENTED, BybitDemoAdapter
from tripwire.exchanges.fake import FakeAdapter
from tripwire.exchanges.hyperliquid import HyperliquidAdapter, pick_stop, utc_day_start_ms
from tripwire.hl.feed import apply_ws_message
from tripwire.orders import CloseOrder, OpenOrder, StopOrder
from tripwire.state import MarketState
from tripwire.trading import parse_order_response, round_px, round_size_down

from .test_state_feed import ACCOUNT, ch_msg

AGENT_KEY = "0x" + "4" * 64
FILLED = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {"statuses": [{"filled": {"totalSz": "0.25", "avgPx": "3499.1", "oid": 77}}]},
    },
}
PLACED = {
    "status": "ok",
    "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 501}}]}},
}
CANCELLED = {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}}
LEVERAGE_OK = {"status": "ok", "response": {"type": "default"}}


class FakeInfo:
    base_url = TESTNET_API_URL
    asset_to_sz_decimals = {4: 4}

    def __init__(self) -> None:
        self.open_orders: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []

    def name_to_asset(self, coin: str) -> int:
        return {"ETH": 4}[coin]

    def frontend_open_orders(self, address: str) -> list[dict[str, Any]]:
        return self.open_orders

    def user_fills_by_time(self, address: str, start: int) -> list[dict[str, Any]]:
        return self.fills


class Poster:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> Any:
        self.payloads.append(payload)
        reply = self.replies.pop(0) if self.replies else FILLED
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def actions(self) -> list[dict[str, Any]]:
        return [p["action"] for p in self.payloads]


def live_state() -> MarketState:
    s = MarketState(account=ACCOUNT)
    apply_ws_message(s, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
    apply_ws_message(s, ch_msg())  # 0.5 ETH long @ 3412, liq 2980
    return s


def adapter(poster: Poster, info: FakeInfo | None = None, **kw: Any) -> HyperliquidAdapter:
    fake_info = info or FakeInfo()
    return HyperliquidAdapter(
        kw.pop("state", None) or live_state(),
        base_url=TESTNET_API_URL,
        account_address=ACCOUNT,
        agent_key=AGENT_KEY,
        info_factory=lambda: fake_info,
        poster=poster,
        **kw,
    )


# --- pure helpers ---------------------------------------------------------------------------


def test_round_size_down() -> None:
    assert round_size_down(0.125, 4) == 0.125
    assert round_size_down(0.125, 2) == 0.12  # down, not half-even / half-up
    assert round_size_down(0.19999, 1) == 0.1
    assert round_size_down(10.0, 0) == 10.0
    assert round_size_down(0.1 + 0.2, 2) == 0.3  # float noise doesn't push it below


def test_round_px_follows_hyperliquid_tick_rules() -> None:
    assert round_px(3319.68, 4) == 3319.7  # 5 significant figures
    assert round_px(0.0556271, 0) == 0.055627  # <= 6 decimals when szDecimals = 0
    assert round_px(96512.34, 5) == 96512.0


@pytest.mark.parametrize(
    ("resp", "ok", "status", "message"),
    [
        (FILLED, True, "filled", "Filled 0.25 @ 3499.1"),
        (PLACED, True, "resting", "Order resting (oid 501)"),
        ({"status": "ok", "response": {"data": {"statuses": ["waitingForTrigger"]}}},
         True, "placed", "Placed (waitingForTrigger)"),
        (CANCELLED, True, "placed", "Placed (success)"),
        ({"status": "ok", "response": {"data": {"statuses": [
            {"error": "Order could not immediately match against any resting orders."}
        ]}}}, False, "rejected", "Order could not immediately match against any resting orders."),
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


def test_long_error_messages_are_shortened() -> None:
    r = parse_order_response({"status": "err", "response": "x" * 500})
    assert len(r.message) == 120 and r.message.endswith("...")


# --- Hyperliquid adapter -------------------------------------------------------------------


def test_refuses_mainnet_and_is_an_exchange_adapter() -> None:
    with pytest.raises(TestnetGuardError):
        HyperliquidAdapter(
            MarketState(), base_url=constants.MAINNET_API_URL, account_address=ACCOUNT
        )
    assert isinstance(adapter(Poster()), ExchangeAdapter)
    assert isinstance(FakeAdapter(MarketState()), ExchangeAdapter)
    assert isinstance(BybitDemoAdapter(), ExchangeAdapter)


def test_trading_needs_account_key_and_not_watch() -> None:
    state = MarketState()
    no_key = HyperliquidAdapter(state, base_url=TESTNET_API_URL, account_address=ACCOUNT)
    assert not no_key.can_trade and no_key.status == "off" and "HL_AGENT_KEY" in no_key.detail
    watch = HyperliquidAdapter(
        state, base_url=TESTNET_API_URL, account_address="", agent_key=AGENT_KEY, watch=True
    )
    assert not watch.can_trade and "watch only" in watch.detail
    assert adapter(Poster()).can_trade


def test_agent_key_is_not_kept_only_its_address() -> None:
    key = "0x" + secrets.token_hex(32)
    a = HyperliquidAdapter(
        MarketState(), base_url=TESTNET_API_URL, account_address=ACCOUNT, agent_key=key
    )
    assert a.agent_address.startswith("0x") and len(a.agent_address) == 42
    assert all(key not in repr(v) for k, v in vars(a).items() if k != "_wallet")


async def test_close_builds_a_reduce_only_ioc_and_signs_for_testnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hyperliquid.utils.signing as signing

    seen: list[bool] = []
    real_sign = signing.sign_l1_action

    def spy(wallet: Any, action: Any, vault: Any, nonce: int, expires: Any, mainnet: bool) -> Any:
        seen.append(mainnet)
        return real_sign(wallet, action, vault, nonce, expires, mainnet)

    monkeypatch.setattr(signing, "sign_l1_action", spy)
    poster = Poster(FILLED)
    a = adapter(poster)
    await a.start()
    assert a.status == "ready" and a.size_decimals("ETH") == 4
    r = await a.close_reduce_only(CloseOrder("ETH", "LONG", 0.5, 0.25))
    assert r.ok and r.status == "filled"
    (payload,) = poster.payloads
    action = payload["action"]
    assert action["type"] == "order" and action["grouping"] == "na"
    (o,) = action["orders"]
    assert o == {
        "a": 4, "b": False, "p": "3319.7", "s": "0.25", "r": True, "t": {"limit": {"tif": "Ioc"}},
    }  # fmt: skip  # sell 0.25 at mid 3494.4 * 0.95, reduce-only
    assert seen == [False]  # signed for TESTNET
    assert set(payload["signature"]) == {"r", "s", "v"} and payload["vaultAddress"] is None


async def test_close_all_uses_the_live_size_and_refuses_a_flipped_side() -> None:
    poster = Poster(FILLED)
    a = adapter(poster)
    await a.close_reduce_only(CloseOrder("ETH", "LONG", 1.0, None))
    assert poster.actions[0]["orders"][0]["s"] == "0.5"
    r = await a.close_reduce_only(CloseOrder("ETH", "SHORT", 1.0, None))
    assert not r.ok and len(poster.payloads) == 1  # nothing sent for the wrong side


async def test_set_sl_places_first_then_cancels_the_old_one() -> None:
    poster = Poster(PLACED, CANCELLED)
    a = adapter(poster)
    r = await a.set_sl(StopOrder("ETH", "LONG", 3412.0, 3300.0, 88, "SL to breakeven"))
    assert r.ok and r.status == "placed" and r.message == "SL set at 3412; old SL cancelled"
    place, cancel = poster.actions
    assert place["grouping"] == "positionTpsl"
    (o,) = place["orders"]
    assert o["b"] is False and o["r"] is True and o["s"] == "0.5"
    assert o["t"] == {"trigger": {"isMarket": True, "triggerPx": "3412", "tpsl": "sl"}}
    assert cancel == {"type": "cancel", "cancels": [{"a": 4, "o": 88}]}


async def test_set_sl_keeps_the_old_stop_if_the_new_one_is_rejected() -> None:
    poster = Poster({"status": "err", "response": "Invalid TP/SL price."})
    a = adapter(poster)
    r = await a.set_sl(StopOrder("ETH", "LONG", 3412.0, 3300.0, 88, "SL to breakeven"))
    assert not r.ok and r.message == "Invalid TP/SL price." and len(poster.payloads) == 1


async def test_place_order_sets_leverage_then_entry_with_sl_and_tp() -> None:
    poster = Poster(LEVERAGE_OK, PLACED)
    a = adapter(poster)
    order = OpenOrder("ETH", "LONG", 100.0, 3.0, 3400.0, 3300.0, 3600.0)
    r = await a.place_order(order)
    assert r.ok and r.status == "resting"
    lev, act = poster.actions
    assert lev == {"type": "updateLeverage", "asset": 4, "isCross": True, "leverage": 3}
    assert act["grouping"] == "normalTpsl"
    entry, sl, tp = act["orders"]
    assert (entry["b"], entry["r"], entry["s"], entry["p"]) == (True, False, "0.0294", "3400")
    assert entry["t"] == {"limit": {"tif": "Gtc"}}
    assert (sl["b"], sl["r"], sl["t"]["trigger"]["tpsl"]) == (False, True, "sl")
    assert (tp["b"], tp["r"], tp["t"]["trigger"]["tpsl"]) == (False, True, "tp")


async def test_errors_map_to_results_not_exceptions() -> None:
    class ClientErrorLike(Exception):
        error_message = "Insufficient margin"

    a = adapter(Poster(ClientErrorLike()))
    r = await a.close_reduce_only(CloseOrder("ETH", "LONG", 1.0, None))
    assert (r.ok, r.status, r.message) == (False, "error", "Insufficient margin")

    def boom() -> Any:
        raise ConnectionError("no route to host")

    b = HyperliquidAdapter(
        live_state(), base_url=TESTNET_API_URL, account_address=ACCOUNT, agent_key=AGENT_KEY,
        info_factory=boom, poster=Poster(),
    )  # fmt: skip
    await b.start()
    assert b.status == "error" and "no route to host" in b.detail
    r = await b.close_reduce_only(CloseOrder("ETH", "LONG", 1.0, None))
    assert not r.ok and "unreachable" in r.message


async def test_info_not_on_testnet_is_refused() -> None:
    class MainnetInfo(FakeInfo):
        base_url = constants.MAINNET_API_URL

    r = await adapter(Poster(), MainnetInfo()).close_reduce_only(
        CloseOrder("ETH", "LONG", 1.0, None)
    )
    assert r.message == "Refused: exchange is not testnet"


async def test_current_stop_and_realized_pnl() -> None:
    info = FakeInfo()
    info.open_orders = [
        {"coin": "ETH", "isTrigger": True, "reduceOnly": True, "orderType": "Stop Market",
         "triggerPx": "3300.0", "oid": 11},
        {"coin": "ETH", "isTrigger": True, "reduceOnly": True, "orderType": "Stop Market",
         "triggerPx": "3350.0", "oid": 12},
        {"coin": "ETH", "isTrigger": True, "reduceOnly": True, "orderType": "Take Profit Market",
         "triggerPx": "3600.0", "oid": 13},
        {"coin": "BTC", "isTrigger": True, "reduceOnly": True, "orderType": "Stop Market",
         "triggerPx": "50000", "oid": 14},
    ]  # fmt: skip
    info.fills = [{"closedPnl": "-30.5", "fee": "0.5"}, {"closedPnl": "10", "fee": "0.25"}]
    a = adapter(Poster(), info)
    assert await a.current_stop("ETH") == StopInfo("ETH", 12, 3350.0)  # tightest for a long
    assert await a.realized_pnl_today() == pytest.approx(-21.25)
    assert pick_stop(info.open_orders, "ETH", "SHORT") == StopInfo("ETH", 11, 3300.0)


def test_utc_day_start() -> None:
    t = dt.datetime(2026, 11, 24, 15, 30, tzinfo=dt.UTC)
    assert utc_day_start_ms(t) == int(dt.datetime(2026, 11, 24, tzinfo=dt.UTC).timestamp() * 1000)


async def test_trading_off_never_posts() -> None:
    poster = Poster()
    a = HyperliquidAdapter(
        live_state(), base_url=TESTNET_API_URL, account_address=ACCOUNT, poster=poster
    )
    r = await a.close_reduce_only(CloseOrder("ETH", "LONG", 1.0, None))
    assert not r.ok and "Trading off" in r.message and poster.payloads == []
    assert hl_mod.HyperliquidAdapter is HyperliquidAdapter


# --- fake + Bybit stub -----------------------------------------------------------------------


async def test_fake_adapter_fills_moves_stops_and_tracks_pnl() -> None:
    state = MarketState()
    apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "1800"}}})
    row = {"coin": "ETH", "szi": "0.5", "entryPx": "1700", "liquidationPx": "1420",
           "leverage": {"type": "cross", "value": 5}, "positionValue": "900"}  # fmt: skip
    fake = FakeAdapter(state, [row], stops={"ETH": 1600.0})
    assert state.positions[0].size == 0.5
    r = await fake.close_reduce_only(CloseOrder("ETH", "LONG", 0.5, 0.25))
    assert r.ok and state.positions[0].size == 0.25
    assert await fake.realized_pnl_today() == pytest.approx(25.0)  # 0.25 * (1800 - 1700)
    old = await fake.current_stop("ETH")
    r = await fake.set_sl(StopOrder("ETH", "LONG", 1700.0, 1600.0, old.oid if old else None, "x"))
    assert r.message == "SL set at 1700; old SL cancelled"
    assert (await fake.current_stop("ETH")).trigger_px == 1700.0  # type: ignore[union-attr]
    await fake.close_reduce_only(CloseOrder("ETH", "LONG", 1.0, None))
    assert state.positions == [] and await fake.current_stop("ETH") is None


async def test_bybit_demo_stub_is_demo_only_and_does_nothing() -> None:
    with pytest.raises(ValueError):
        BybitDemoAdapter("https://api.bybit.com")
    b = BybitDemoAdapter()
    assert not b.can_trade and b.status == "off"
    r = await b.close_reduce_only(CloseOrder("ETH", "LONG", 1.0, None))
    assert (r.ok, r.message) == (False, NOT_IMPLEMENTED)
    assert [p async for p in b.stream_positions()] == [[]]
