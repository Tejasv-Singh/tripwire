"""HUD frame builder, navigation and throttle math."""

from __future__ import annotations

import pytest

from tripwire.hl.feed import apply_ws_message
from tripwire.hud import apply_nav, build_hud_frame, next_send_delay
from tripwire.state import MarketState

from .test_state_feed import ACCOUNT, ch_msg


def test_frame_without_account_shows_reference_price() -> None:
    s = MarketState()
    s.set_connected(True)
    s.touch(now=100.0)
    apply_ws_message(s, {"channel": "allMids", "data": {"mids": {"ETH": "1721.25"}}})
    f = build_hud_frame(s, index=0, seq=7, now_ms=1_000, now_mono=100.25)
    assert f["type"] == "hud" and f["v"] == 2 and f["seq"] == 7 and f["ts"] == 1_000
    assert f["network"] == "testnet"
    assert f["feed"] == {"connected": True, "age_ms": 250}
    assert f["account"] is False
    assert f["position"] is None
    assert f["view"] == {"index": 0, "count": 0}
    # no markPx yet -> mark falls back to mid and says so
    assert f["ref"] == {"coin": "ETH", "mid": 1721.25, "mark": 1721.25, "mark_source": "mid"}
    assert f["ui"] == {"mode": "hud"}
    assert f["trading"] == {"enabled": False, "status": "off"}


def test_frame_ref_prefers_markpx() -> None:
    s = MarketState()
    apply_ws_message(
        s,
        {
            "channel": "activeAssetCtx",
            "data": {"coin": "ETH", "ctx": {"markPx": "1721.9", "midPx": "1721.75"}},
        },
    )
    f = build_hud_frame(s, index=0, seq=1)
    assert f["ref"]["mark"] == 1721.9 and f["ref"]["mark_source"] == "markPx"


def test_frame_with_position_and_index_clamping() -> None:
    s = MarketState(account=ACCOUNT)
    apply_ws_message(s, ch_msg())
    f = build_hud_frame(s, index=5, seq=1)
    assert f["view"] == {"index": 0, "count": 1}
    assert f["position"]["coin"] == "ETH" and f["position"]["side"] == "LONG"


def test_locked_coin_follows_its_position_and_disappears_with_it() -> None:
    s = MarketState(account=ACCOUNT)
    apply_ws_message(s, ch_msg(coin="SOL"))
    f = build_hud_frame(s, index=0, seq=1, lock_coin="SOL")
    assert f["position"]["coin"] == "SOL"
    # a new BTC position sorts before SOL: the locked client must still see SOL
    msg = ch_msg(coin="SOL")
    aps = msg["data"]["clearinghouseState"]["assetPositions"]
    aps.append(ch_msg(coin="BTC")["data"]["clearinghouseState"]["assetPositions"][0])
    apply_ws_message(s, msg)
    f = build_hud_frame(s, index=0, seq=2, lock_coin="SOL")
    assert f["view"] == {"index": 1, "count": 2} and f["position"]["coin"] == "SOL"
    # SOL closed while locked -> no position (the ui explains), never a different coin
    apply_ws_message(s, ch_msg(coin="BTC"))
    f = build_hud_frame(s, index=1, seq=3, lock_coin="SOL")
    assert f["position"] is None


@pytest.mark.parametrize(
    ("index", "count", "action", "expected"),
    [
        (0, 3, "next", 1), (2, 3, "next", 0), (0, 3, "prev", 2),
        (1, 3, "tap", 1), (0, 0, "next", 0), (4, 2, "tap", 0),
    ],
)  # fmt: skip
def test_apply_nav_wraps(index: int, count: int, action: str, expected: int) -> None:
    assert apply_nav(index, count, action) == expected


def test_next_send_delay_enforces_min_interval() -> None:
    assert next_send_delay(last_sent=10.0, now=10.1, min_interval_s=0.5) == pytest.approx(0.4)
    assert next_send_delay(last_sent=10.0, now=10.6, min_interval_s=0.5) == 0.0
    assert next_send_delay(last_sent=float("-inf"), now=0.0, min_interval_s=0.5) == 0.0
