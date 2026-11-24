"""ActionFlow: the menu -> two-tap confirm -> order state machine (pure, no network, no clock)."""

from __future__ import annotations

import dataclasses

import pytest

from tripwire.actions import CONFIRM_WINDOW_S, MENU, MIN_CONFIRM_GAP_S, ActionFlow
from tripwire.hl.positions import Position
from tripwire.trading import OrderResult

POS = Position(
    coin="ETH", side="LONG", size=0.5, entry=3412.0, mark=3500.0, upnl=41.2, upnl_pct=2.415,
    roe_pct=12.07, liq_px=2980.0, liq_dist_pct=14.86, leverage=5.0, mid=3494.4,
    mark_source="markPx", leverage_type="cross",
)  # fmt: skip


def open_menu(flow: ActionFlow, now: float = 0.0) -> None:
    flow.handle("tap", position=POS, now=now, trading=True)
    assert flow.mode == "menu" and flow.coin == "ETH" and flow.cursor == 0


def arm(flow: ActionFlow, cursor: int, now: float = 1.0, decimals: int | None = 4) -> None:
    open_menu(flow)
    for _ in range(cursor):
        flow.handle("next", position=POS, now=now, trading=True)
    out = flow.handle("tap", position=POS, now=now, trading=True, size_decimals=decimals)
    assert flow.mode == "confirm" and out.timer_s == CONFIRM_WINDOW_S


def test_menu_is_reduce_only_closes_plus_back() -> None:
    assert [m.label for m in MENU] == ["Close 25%", "Close 50%", "Close 100%", "Back"]
    assert all(m.fraction is None or 0 < m.fraction <= 1 for m in MENU)


def test_hud_tap_needs_a_position_and_swipes_navigate() -> None:
    flow = ActionFlow()
    out = flow.handle("tap", position=None, now=0, trading=True)
    assert flow.mode == "hud" and not out.ok and out.note == "no open position"
    assert flow.handle("next", position=POS, now=0, trading=True).nav == "next"
    assert flow.handle("prev", position=POS, now=0, trading=True).nav == "prev"


def test_menu_cursor_wraps_and_back_returns_to_hud() -> None:
    flow = ActionFlow()
    open_menu(flow)
    flow.handle("prev", position=POS, now=0, trading=True)
    assert flow.cursor == len(MENU) - 1
    flow.handle("tap", position=POS, now=0, trading=True)  # "Back" item
    assert flow.mode == "hud" and flow.coin is None
    open_menu(flow)
    flow.handle("back", position=POS, now=0, trading=True)  # double tap
    assert flow.mode == "hud"


def test_first_tap_only_arms_and_shows_exact_rounded_size() -> None:
    flow = ActionFlow()
    arm(flow, cursor=0)  # Close 25 % of 0.5 -> 0.125 (ETH szDecimals = 4)
    ui = flow.ui(now=2.0, trading=True)
    assert ui["mode"] == "confirm" and ui["label"] == "Close 25%" and ui["side"] == "LONG"
    assert ui["size"] == 0.125 and ui["exact"] is True and ui["all"] is False
    assert ui["est_px"] == 3500.0 and ui["slippage_pct"] == 5.0
    assert ui["expires_in_ms"] == 4000


def test_partial_size_rounds_down_never_up() -> None:
    flow = ActionFlow()
    arm(flow, cursor=0, decimals=1)  # 0.125 -> 0.1, never 0.2
    assert flow.ui(1.0, trading=True)["size"] == 0.1


def test_second_tap_inside_window_sends_exactly_what_was_shown() -> None:
    flow = ActionFlow()
    arm(flow, cursor=1)  # Close 50 %
    out = flow.handle("tap", position=POS, now=2.0, trading=True)
    assert flow.mode == "sending" and out.execute is not None
    assert (out.execute.coin, out.execute.side, out.execute.fraction) == ("ETH", "LONG", 0.5)
    assert out.execute.size == 0.25


def test_close_100_sends_size_none_meaning_the_whole_live_position() -> None:
    flow = ActionFlow()
    arm(flow, cursor=2)
    assert flow.ui(1.0, trading=True)["all"] is True
    out = flow.handle("tap", position=POS, now=2.0, trading=True)
    assert out.execute is not None and out.execute.size is None


def test_bounced_second_tap_is_not_a_confirm() -> None:
    flow = ActionFlow()
    arm(flow, cursor=1, now=1.0)
    out = flow.handle("tap", position=POS, now=1.0 + MIN_CONFIRM_GAP_S / 2, trading=True)
    assert flow.mode == "confirm" and out.execute is None and not out.ok


def test_late_second_tap_expires_instead_of_sending() -> None:
    flow = ActionFlow()
    arm(flow, cursor=1, now=1.0)
    out = flow.handle("tap", position=POS, now=1.0 + CONFIRM_WINDOW_S + 0.1, trading=True)
    assert flow.mode == "menu" and out.execute is None
    assert flow.ui(10, trading=True)["note"] == "Confirm expired - not sent"


def test_confirm_timer_expires_back_to_menu_and_stale_timers_are_ignored() -> None:
    flow = ActionFlow()
    open_menu(flow)
    out = flow.handle("tap", position=POS, now=1.0, trading=True, size_decimals=4)
    stale = out.token
    flow.handle("next", position=POS, now=1.5, trading=True)  # swipe = cancel -> menu
    assert flow.mode == "menu"
    assert flow.on_timer(stale) is False  # old timer must not touch the new screen
    out = flow.handle("tap", position=POS, now=2.0, trading=True, size_decimals=4)  # re-arm
    assert flow.on_timer(out.token) is True and flow.mode == "menu"


def test_swipe_or_double_tap_cancels_without_sending() -> None:
    flow = ActionFlow()
    arm(flow, cursor=1)
    out = flow.handle("prev", position=POS, now=2.0, trading=True)
    assert flow.mode == "menu" and out.execute is None
    flow.handle("tap", position=POS, now=3.0, trading=True)  # re-arm the same item
    out = flow.handle("back", position=POS, now=4.0, trading=True)
    assert flow.mode == "hud" and out.execute is None


def test_position_changing_between_taps_blocks_the_order() -> None:
    flow = ActionFlow()
    arm(flow, cursor=1)
    flipped = dataclasses.replace(POS, side="SHORT")
    out = flow.handle("tap", position=flipped, now=2.0, trading=True)
    assert out.execute is None and flow.mode == "result"
    assert flow.ui(2.0, trading=True)["message"] == "Position changed - NOT sent"
    flow2 = ActionFlow()
    arm(flow2, cursor=1)
    out = flow2.handle("tap", position=None, now=2.0, trading=True)  # closed elsewhere
    assert out.execute is None and flow2.mode == "result"


def test_partial_close_is_capped_at_the_live_size() -> None:
    flow = ActionFlow()
    arm(flow, cursor=1)  # shown 0.25
    smaller = dataclasses.replace(POS, size=0.2)  # partly closed elsewhere meanwhile
    out = flow.handle("tap", position=smaller, now=2.0, trading=True)
    assert out.execute is not None and out.execute.size == 0.2


def test_trading_off_never_reaches_confirm() -> None:
    flow = ActionFlow()
    open_menu(flow)
    out = flow.handle("tap", position=POS, now=1.0, trading=False)
    assert flow.mode == "result" and out.execute is None and not out.ok
    assert "HL_AGENT_KEY" in flow.ui(1.0, trading=False)["message"]


def test_tiny_partial_that_rounds_to_zero_is_refused() -> None:
    tiny = dataclasses.replace(POS, size=0.0002)
    flow = ActionFlow()
    flow.handle("tap", position=tiny, now=0, trading=True)
    out = flow.handle("tap", position=tiny, now=1, trading=True, size_decimals=4)  # 25 % -> 0
    assert flow.mode == "result" and out.execute is None


def test_sending_ignores_input_then_result_returns_to_hud() -> None:
    flow = ActionFlow()
    arm(flow, cursor=1)
    flow.handle("tap", position=POS, now=2.0, trading=True)
    assert flow.handle("back", position=POS, now=2.1, trading=True).note == "busy - order in flight"
    assert flow.mode == "sending"
    out = flow.finish(OrderResult(True, "filled", "Filled 0.25 @ 3499.1", 0.25, 3499.1, 7))
    ui = flow.ui(3.0, trading=True)
    assert ui == {
        "mode": "result", "coin": "ETH", "ok": True, "status": "filled",
        "message": "Filled 0.25 @ 3499.1", "label": "Close 50%",
    }  # fmt: skip
    assert flow.on_timer(out.token) is True and flow.mode == "hud"


@pytest.mark.parametrize("intent", ["tap", "back", "next", "prev"])
def test_any_input_dismisses_the_result(intent: str) -> None:
    flow = ActionFlow()
    arm(flow, cursor=1)
    flow.handle("tap", position=POS, now=2.0, trading=True)
    flow.finish(OrderResult(False, "rejected", "nope"))
    flow.handle(intent, position=POS, now=3.0, trading=True)  # type: ignore[arg-type]
    assert flow.mode == "hud"
