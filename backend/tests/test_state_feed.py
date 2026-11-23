"""MarketState updates + Hyperliquid WS message routing (no network)."""

from __future__ import annotations

from typing import Any

import pytest
from hyperliquid.utils import constants

from tripwire.config import TestnetGuardError
from tripwire.hl.feed import HyperliquidFeed, apply_ws_message, ws_url_for
from tripwire.state import MarketState

ACCOUNT = "0xabcdef0123456789abcdef0123456789abcdef01"


def ch_msg(user: str = ACCOUNT, szi: str = "0.5", coin: str = "ETH") -> dict[str, Any]:
    return {
        "channel": "clearinghouseState",
        "data": {
            "dex": "",
            "user": user,
            "clearinghouseState": {
                "assetPositions": [
                    {
                        "type": "oneWay",
                        "position": {
                            "coin": coin,
                            "szi": szi,
                            "entryPx": "3412.0",
                            "liquidationPx": "2980.0",
                            "leverage": {"type": "cross", "value": 5},
                            "positionValue": "1747.2",
                        },
                    }
                ]
            },
        },
    }


class Counter:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> None:
        self.n += 1


def test_ws_url_and_guard() -> None:
    assert ws_url_for(constants.TESTNET_API_URL) == "wss://api.hyperliquid-testnet.xyz/ws"
    with pytest.raises(TestnetGuardError):
        HyperliquidFeed(constants.MAINNET_API_URL, MarketState())


def test_all_mids_only_wakes_listeners_for_watched_coins() -> None:
    s, hits = MarketState(), Counter()
    s.add_listener(hits)
    apply_ws_message(s, {"channel": "allMids", "data": {"mids": {"BTC": "61280.0"}}})
    assert hits.n == 0 and s.mids["BTC"] == 61280.0  # stored, but nobody woken
    apply_ws_message(s, {"channel": "allMids", "data": {"mids": {"ETH": "1721.25"}}})
    assert hits.n == 1 and s.mids["ETH"] == 1721.25
    apply_ws_message(s, {"channel": "allMids", "data": {"mids": {"ETH": "1721.25"}}})
    assert hits.n == 1  # unchanged value -> no wake


def test_active_asset_ctx_sets_mark_and_mid() -> None:
    s = MarketState()
    msg = {
        "channel": "activeAssetCtx",
        "data": {"coin": "ETH", "ctx": {"markPx": "1721.9", "midPx": "1721.75"}},
    }
    apply_ws_message(s, msg)
    assert s.marks["ETH"] == 1721.9 and s.mids["ETH"] == 1721.75


def test_clearinghouse_for_our_account_creates_positions() -> None:
    s, hits = MarketState(account=ACCOUNT), Counter()
    s.add_listener(hits)
    apply_ws_message(s, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
    apply_ws_message(s, ch_msg())
    assert [p.coin for p in s.positions] == ["ETH"]
    assert s.positions[0].upnl == pytest.approx(41.2)
    assert s.watched_coins() == {"ETH"}
    assert hits.n == 2


def test_clearinghouse_for_other_user_is_ignored() -> None:
    s = MarketState(account=ACCOUNT)
    apply_ws_message(s, ch_msg(user="0x" + "1" * 40))
    assert s.positions == []


def test_position_coin_becomes_watched() -> None:
    s = MarketState(account=ACCOUNT)
    apply_ws_message(s, ch_msg(coin="SOL"))
    assert s.watched_coins() == {"ETH", "SOL"}
    apply_ws_message(s, ch_msg(coin="SOL", szi="0.0"))  # position closed
    assert s.positions == [] and s.watched_coins() == {"ETH"}


def test_unknown_and_malformed_messages_are_harmless() -> None:
    s = MarketState()
    for msg in [
        {"channel": "subscriptionResponse", "data": {"method": "subscribe"}},
        {"channel": "pong"},
        {"channel": "error", "data": "boom"},
        {"channel": "allMids", "data": "garbage"},
        {"channel": "activeAssetCtx", "data": {"coin": 5}},
        {"nope": True},
    ]:
        apply_ws_message(s, msg)
    assert s.mids == {} and s.marks == {} and s.version == 0


def test_set_connected_notifies_once_per_change() -> None:
    s, hits = MarketState(), Counter()
    s.add_listener(hits)
    s.set_connected(True)
    s.set_connected(True)
    s.set_connected(False)
    assert hits.n == 2
