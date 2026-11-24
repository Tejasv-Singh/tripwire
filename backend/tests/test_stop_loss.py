"""Move stop-loss (item 3): SL submenu, breakeven / tighter-1% maths, policy blocks, end to end."""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tripwire.actions import ActionFlow
from tripwire.db import Store
from tripwire.exchanges.base import StopInfo
from tripwire.exchanges.fake import FakeAdapter
from tripwire.hl.feed import apply_ws_message
from tripwire.hl.positions import Position
from tripwire.main import create_app
from tripwire.orders import Order, StopOrder
from tripwire.policy import Decision, KillSwitch, PolicyGuard, PolicyLimits
from tripwire.state import MarketState

from .test_app import CODE, ETH_ROW, recv, settings

LONG = Position(
    coin="ETH", side="LONG", size=0.5, entry=1700.0, mark=1750.0, upnl=25.0, upnl_pct=2.94,
    roe_pct=14.7, liq_px=1420.0, liq_dist_pct=18.9, leverage=5.0, mid=1750.0,
    mark_source="markPx", leverage_type="cross",
)  # fmt: skip
SHORT = dataclasses.replace(LONG, side="SHORT", entry=1800.0, liq_px=2100.0)
GUARD = PolicyGuard(PolicyLimits(), KillSwitch(False, None))


def precheck_for(p: Position) -> Any:
    def check(order: Order) -> Decision:
        return GUARD.check(order, position=p, mark=p.mark)

    return check


def to_sl_menu(flow: ActionFlow, p: Position, stop: StopInfo | None) -> None:
    flow.handle("tap", position=p, now=0, trading=True)  # actions menu
    for _ in range(3):
        flow.handle("next", position=p, now=0, trading=True)  # -> "Move SL..."
    out = flow.handle("tap", position=p, now=0, trading=True)
    assert flow.mode == "menu" and flow.kind == "sl" and out.fetch_stop == p.coin
    assert flow.ui(0, trading=True)["sl_loaded"] is False
    assert flow.set_stop(p.coin, stop) is True
    ui = flow.ui(0, trading=True)
    assert ui["sl_loaded"] is True and ui["current_sl"] == (stop.trigger_px if stop else None)


def pick(flow: ActionFlow, p: Position, index: int, now: float = 1.0) -> Any:
    for _ in range(index):
        flow.handle("next", position=p, now=now, trading=True)
    return flow.handle(
        "tap", position=p, now=now, trading=True, size_decimals=4, precheck=precheck_for(p)
    )


def test_sl_items_wait_for_the_current_stop() -> None:
    flow = ActionFlow()
    flow.handle("tap", position=LONG, now=0, trading=True)
    for _ in range(3):
        flow.handle("next", position=LONG, now=0, trading=True)
    flow.handle("tap", position=LONG, now=0, trading=True)
    out = flow.handle("tap", position=LONG, now=1, trading=True)  # before the lookup answered
    assert (
        flow.mode == "menu"
        and not out.ok
        and out.note == "loading the current stop-loss - tap again"
    )


def test_breakeven_for_a_long_in_profit() -> None:
    flow = ActionFlow()
    to_sl_menu(flow, LONG, StopInfo("ETH", 11, 1600.0))
    out = pick(flow, LONG, 0)
    assert flow.mode == "confirm" and out.timer_s
    ui = flow.ui(2.0, trading=True)
    assert (ui["action"], ui["label"], ui["old_sl"], ui["new_sl"]) == (
        "stop",
        "SL to breakeven",
        1600.0,
        1700.0,
    )
    sent = flow.handle("tap", position=LONG, now=2.0, trading=True)
    assert sent.execute == StopOrder("ETH", "LONG", 1700.0, 1600.0, 11, "SL to breakeven")


def test_breakeven_while_losing_is_blocked_with_the_reason() -> None:
    losing = dataclasses.replace(LONG, mark=1650.0)
    flow = ActionFlow()
    to_sl_menu(flow, losing, StopInfo("ETH", 11, 1600.0))
    out = pick(flow, losing, 0)
    assert flow.mode == "result" and out.execute is None
    ui = flow.ui(1, trading=True)
    assert ui["status"] == "blocked"
    assert ui["message"] == "Blocked: SL 1,700 is above mark 1,650 - it would fire at once"


def test_tighter_one_percent_moves_toward_the_price() -> None:
    flow = ActionFlow()
    to_sl_menu(flow, LONG, StopInfo("ETH", 11, 1600.0))
    pick(flow, LONG, 1)  # 1600 + 1% of 1750 = 1617.5
    assert flow.ui(2.0, trading=True)["new_sl"] == 1617.5
    out = flow.handle("tap", position=LONG, now=2.0, trading=True)
    assert isinstance(out.execute, StopOrder) and out.execute.trigger_px == 1617.5


def test_tighter_for_a_short_moves_down() -> None:
    flow = ActionFlow()
    to_sl_menu(flow, SHORT, StopInfo("ETH", 12, 1900.0))
    pick(flow, SHORT, 1)  # 1900 - 17.5
    assert flow.ui(2.0, trading=True)["new_sl"] == 1882.5
    flow2 = ActionFlow()
    to_sl_menu(flow2, SHORT, StopInfo("ETH", 12, 1900.0))
    pick(flow2, SHORT, 0)  # breakeven 1800 is above mark 1750: protective for a short
    assert flow2.mode == "confirm" and flow2.ui(2.0, trading=True)["new_sl"] == 1800.0


def test_tighter_needs_an_existing_stop_and_cannot_cross_the_mark() -> None:
    flow = ActionFlow()
    to_sl_menu(flow, LONG, None)
    pick(flow, LONG, 1)
    assert flow.mode == "result"
    assert flow.ui(1, trading=True)["message"] == "No stop-loss to tighten - use SL to breakeven"
    near = ActionFlow()
    to_sl_menu(near, LONG, StopInfo("ETH", 11, 1740.0))  # 1740 + 17.5 = 1757.5 > mark 1750
    pick(near, LONG, 1)
    assert near.ui(1, trading=True)["status"] == "blocked"


def test_breakeven_that_would_loosen_is_blocked() -> None:
    flow = ActionFlow()
    to_sl_menu(flow, LONG, StopInfo("ETH", 11, 1720.0))  # already above entry 1700
    pick(flow, LONG, 0)
    assert flow.ui(1, trading=True)["message"].startswith(
        "Blocked: SL 1,700 is not tighter than 1,720"
    )


def test_back_and_cancel_navigation_in_the_sl_submenu() -> None:
    flow = ActionFlow()
    to_sl_menu(flow, LONG, StopInfo("ETH", 11, 1600.0))
    pick(flow, LONG, 2)  # "Back" -> actions menu
    assert flow.mode == "menu" and flow.kind == "actions"
    to_sl_menu_again = ActionFlow()
    to_sl_menu(to_sl_menu_again, LONG, StopInfo("ETH", 11, 1600.0))
    pick(to_sl_menu_again, LONG, 0)
    to_sl_menu_again.handle("next", position=LONG, now=2.0, trading=True)  # swipe = cancel
    assert to_sl_menu_again.mode == "menu" and to_sl_menu_again.kind == "sl"
    assert to_sl_menu_again.ui(2, trading=True)["note"] == "Cancelled - not sent"


def test_position_flip_between_taps_blocks_the_stop() -> None:
    flow = ActionFlow()
    to_sl_menu(flow, LONG, StopInfo("ETH", 11, 1600.0))
    pick(flow, LONG, 0)
    out = flow.handle("tap", position=SHORT, now=2.0, trading=True)
    assert (
        out.execute is None and flow.ui(2, trading=True)["message"] == "Position changed - NOT sent"
    )


# --- end to end through /ws with the fake exchange -----------------------------------------


@pytest.fixture
def sl_client(tmp_path: Path) -> Iterator[tuple[TestClient, FakeAdapter]]:
    holder: list[FakeAdapter] = []

    def make(state: MarketState) -> FakeAdapter:
        apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
        holder.append(FakeAdapter(state, [ETH_ROW], stops={"ETH": 3300.0}))
        return holder[0]

    app = create_app(
        settings(tmp_path), run_feed=False, run_bot=False, heartbeat_s=0.2, make_adapter=make
    )
    with TestClient(app) as c:
        yield c, holder[0]


def test_sl_to_breakeven_end_to_end(sl_client: tuple[TestClient, FakeAdapter]) -> None:
    client, fake = sl_client
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.send_json({"type": "intent", "action": "tap"})
        recv(ws, "ack")
        for _ in range(3):
            ws.send_json({"type": "intent", "action": "next"})
            recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "tap"})  # Move SL...
        assert recv(ws, "ack")["mode"] == "menu"
        for _ in range(20):  # the current SL arrives from the (fake) exchange
            sl_menu = recv(ws, "hud", mode="menu")["ui"]
            if sl_menu.get("sl_loaded"):
                break
        assert sl_menu["kind"] == "sl" and sl_menu["current_sl"] == 3300.0
        ws.send_json({"type": "intent", "action": "tap"})  # SL to breakeven: arm
        assert recv(ws, "ack")["mode"] == "confirm"
        confirm = recv(ws, "hud", mode="confirm")["ui"]
        assert (confirm["action"], confirm["old_sl"], confirm["new_sl"]) == ("stop", 3300.0, 3412.0)
        assert fake.calls == []
        time.sleep(0.35)
        ws.send_json({"type": "intent", "action": "tap"})  # send
        order = recv(ws, "order")
        assert order["kind"] == "stop" and order["status"] == "placed" and order["fraction"] is None
        result = recv(ws, "hud", mode="result")["ui"]
        assert result["message"] == "SL set at 3412; old SL cancelled"
    assert fake.stops["ETH"].trigger_px == 3412.0
    rows = client.app.state.store.recent_orders()  # type: ignore[attr-defined]
    assert rows[0]["kind"] == "stop" and rows[0]["status"] == "placed"


def test_order_log_migration_adds_kind(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as conn:  # a Day-2 schema without the `kind` column
        conn.execute(
            "CREATE TABLE order_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,"
            " client TEXT NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL, fraction REAL NOT NULL,"
            " size REAL, ok INTEGER NOT NULL, status TEXT NOT NULL, filled_size REAL, avg_px REAL,"
            " oid INTEGER, message TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO order_log (ts, client, coin, side, fraction, ok, status, message)"
            " VALUES (1, 'c', 'ETH', 'LONG', 0.5, 1, 'filled', 'old row')"
        )
    store = Store(db)
    store.init()
    row: dict[str, Any] = dict(
        client="c", coin="ETH", side="LONG", fraction=0.0, size=None, ok=True, status="placed",
        filled_size=None, avg_px=None, oid=5, message="SL", kind="stop",
    )  # fmt: skip
    store.log_order(**row)
    rows = store.recent_orders()
    assert [r["kind"] for r in rows] == ["stop", "close"]  # old rows default to "close"
