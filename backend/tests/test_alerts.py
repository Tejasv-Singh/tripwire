"""Liquidation alerts (item 2): engine, delivery (glasses vs Telegram DM), flow, end to end."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import tripwire.risk as risk_mod
from tripwire.actions import ActionFlow
from tripwire.db import Store
from tripwire.exchanges.fake import FakeAdapter
from tripwire.hl.feed import apply_ws_message
from tripwire.hl.positions import Position
from tripwire.main import create_app
from tripwire.risk import Alert, AlertService, RiskEngine
from tripwire.state import MarketState

from .test_app import CODE, ETH_ROW, recv, settings

P = Position(
    coin="ETH", side="LONG", size=0.5, entry=1700.0, mark=1500.0, upnl=-100.0, upnl_pct=-11.8,
    roe_pct=-58.8, liq_px=1420.0, liq_dist_pct=5.33, leverage=5.0, mid=1500.0,
    mark_source="markPx", leverage_type="cross",
)  # fmt: skip
ALERT = Alert("ETH", "LONG", 0.5, 1420.0, 4.2, 1482.0, 5.0)


def at(dist: float, **kw: Any) -> Position:
    return dataclasses.replace(P, liq_dist_pct=dist, **kw)


# --- engine ---------------------------------------------------------------------------------


def test_fires_once_inside_and_rearms_only_past_the_hysteresis() -> None:
    e = RiskEngine(threshold_pct=5.0, hysteresis_pct=1.0)
    assert e.evaluate([at(5.4)], 0) == []
    (a,) = e.evaluate([at(4.9)], 1)
    assert (a.coin, a.side, a.liq_dist_pct, a.threshold_pct) == ("ETH", "LONG", 4.9, 5.0)
    assert e.evaluate([at(4.0)], 2) == []  # still inside: no repeat
    assert e.evaluate([at(5.9)], 3) == []  # out, but not past 5 + 1: still disarmed
    assert e.evaluate([at(4.5)], 4) == []
    assert e.evaluate([at(6.0)], 5) == []  # >= 6.0: re-armed
    assert len(e.evaluate([at(4.8)], 6)) == 1


def test_a_price_wobbling_around_the_threshold_alerts_once() -> None:
    e = RiskEngine(threshold_pct=5.0, hysteresis_pct=1.0)
    fired = sum(len(e.evaluate([at(d)], i)) for i, d in enumerate([5.1, 4.9, 5.2, 4.8, 5.5, 4.95]))
    assert fired == 1 and e.fired == 1


def test_snooze_silences_for_5_minutes_then_fires_again_if_still_inside() -> None:
    e = RiskEngine(threshold_pct=5.0, snooze_s=300)
    assert len(e.evaluate([at(4.0)], 0)) == 1
    e.snooze("ETH", "LONG", now=10)
    assert e.evaluate([at(3.0)], 100) == [] and e.evaluate([at(3.0)], 309) == []
    assert "ETH LONG" in e.status(200)["snoozed"]
    assert len(e.evaluate([at(3.0)], 311)) == 1


def test_mid_fallback_and_missing_liq_are_ignored() -> None:
    e = RiskEngine(threshold_pct=5.0)
    assert e.evaluate([at(1.0, mark_source="mid")], 0) == []  # liquidation runs on mark
    assert e.evaluate([dataclasses.replace(P, liq_px=None, liq_dist_pct=None)], 0) == []


def test_changing_the_threshold_rearms_and_closed_positions_are_forgotten() -> None:
    e = RiskEngine(threshold_pct=5.0)
    assert e.evaluate([at(8.0)], 0) == []
    e.set_threshold(10.0)
    assert len(e.evaluate([at(8.0)], 1)) == 1  # "lowering the bar" fires right away
    e.evaluate([], 2)  # position closed
    assert e.status(2)["inside"] == []
    short = at(2.0, side="SHORT")
    assert len(e.evaluate([short, at(2.0)], 3)) == 2  # per coin AND side


# --- delivery -------------------------------------------------------------------------------


class Notifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send_owner(self, text: str) -> bool:
        self.sent.append(text)
        return self.ok


def service(glasses: int, notifier: Notifier | None, store: Store | None = None) -> AlertService:
    return AlertService(
        RiskEngine(threshold_pct=5.0),
        FakeAdapter(MarketState()),
        glasses=lambda _a: glasses,
        notifier=notifier,
        store=store,
        clock=lambda: 0.0,
    )


async def test_connected_glasses_get_it_and_telegram_does_not() -> None:
    n = Notifier()
    svc = service(1, n)
    assert await svc.deliver(ALERT) == "glasses (1)" and n.sent == []


async def test_without_glasses_the_owner_gets_a_dm() -> None:
    n = Notifier()
    svc = service(0, n)
    assert await svc.deliver(ALERT) == "telegram"
    (text,) = n.sent
    assert text.startswith("▲ RISK  ETH-PERP LONG 0.5")
    assert "Liquidation 1,420 is 4.2% from mark 1,482 (alert at 5%)" in text
    assert svc.last_delivery == {"alert": ALERT.to_dict(), "delivered_to": "telegram"}


async def test_nowhere_when_no_glasses_and_no_owner() -> None:
    assert (
        await service(0, Notifier(ok=False)).deliver(ALERT)
        == "nowhere (no glasses, no Telegram owner)"
    )
    assert await service(0, None).deliver(ALERT) == "nowhere (no glasses, no Telegram owner)"


def test_threshold_is_persisted(tmp_path: Path) -> None:
    store = Store(tmp_path / "a.sqlite3")
    store.init()
    service(0, None, store).set_threshold(12.5)
    fresh = service(0, None, store)
    fresh.load_threshold()
    assert fresh.engine.threshold_pct == 12.5


async def test_run_loop_reacts_to_position_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(risk_mod, "TICK_S", 0.05)
    state = MarketState()
    apply_ws_message(
        state, {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {"markPx": "3494.4"}}}
    )
    fake = FakeAdapter(state, [ETH_ROW])  # liq 2980: 14.7% away
    got: list[Alert] = []
    svc = AlertService(
        RiskEngine(threshold_pct=5.0), fake, glasses=lambda a: got.append(a) or 1, notifier=None
    )
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.1)
    assert got == []
    apply_ws_message(
        state, {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {"markPx": "3100"}}}
    )
    await asyncio.sleep(0.15)  # 3100 vs liq 2980 = 3.9 % -> fires
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(got) == 1 and got[0].liq_dist_pct == pytest.approx(3.87, abs=0.01)


# --- flow -----------------------------------------------------------------------------------


def test_alert_takes_over_the_hud_with_its_own_menu() -> None:
    flow = ActionFlow()
    assert flow.raise_alert(ALERT) is True
    ui = flow.ui(0, trading=True)
    assert ui["mode"] == "menu" and ui["kind"] == "alert" and ui["coin"] == "ETH"
    assert ui["items"] == ["Move SL", "Close 50%", "Close all", "Snooze 5m"]
    assert ui["alert"]["liq_dist_pct"] == 4.2


def test_alert_waits_for_a_confirm_in_progress() -> None:
    flow = ActionFlow()
    flow.handle("tap", position=P, now=0, trading=True)
    flow.handle("tap", position=P, now=1, trading=True, size_decimals=4)  # arm Close 25%
    assert flow.mode == "confirm"
    assert flow.raise_alert(ALERT) is False and flow.mode == "confirm"  # not hijacked
    flow.handle("back", position=P, now=2, trading=True)  # cancel the confirm
    assert flow.mode == "menu" and flow.kind == "alert"  # ...and the alert appears


def test_snooze_item_returns_to_hud_and_asks_for_a_snooze() -> None:
    flow = ActionFlow()
    flow.raise_alert(ALERT)
    for _ in range(3):
        flow.handle("next", position=P, now=0, trading=True)
    out = flow.handle("tap", position=P, now=0, trading=True)
    assert (
        flow.mode == "hud" and out.snooze == ("ETH", "LONG") and out.note == "alert snoozed 5 min"
    )


def test_alert_actions_reuse_the_two_tap_confirm() -> None:
    flow = ActionFlow()
    flow.raise_alert(ALERT)
    flow.handle("next", position=P, now=0, trading=True)  # Close 50%
    flow.handle("tap", position=P, now=1, trading=True, size_decimals=4)
    assert flow.mode == "confirm" and flow.ui(1, trading=True)["fraction"] == 0.5
    flow.handle("prev", position=P, now=2, trading=True)  # swipe: cancel -> back to the alert
    assert flow.kind == "alert" and flow.note == "Cancelled - not sent"
    flow.handle("next", position=P, now=2, trading=True)  # Close all
    flow.handle("tap", position=P, now=3, trading=True, size_decimals=4)
    out = flow.handle("tap", position=P, now=3.5, trading=True)
    assert out.execute is not None and out.execute.kind == "close" and out.execute.fraction == 1.0


def test_move_sl_from_an_alert_comes_back_to_the_alert() -> None:
    flow = ActionFlow()
    flow.raise_alert(ALERT)
    out = flow.handle("tap", position=P, now=0, trading=True)  # Move SL
    assert flow.kind == "sl" and out.fetch_stop == "ETH"
    flow.handle("prev", position=P, now=0, trading=True)  # cursor -> Back
    flow.handle("tap", position=P, now=0, trading=True)
    assert flow.kind == "alert"


def test_watch_mode_alert_is_informational() -> None:
    flow = ActionFlow()
    flow.raise_alert(ALERT)
    flow.handle("next", position=P, now=0, trading=False, watch=True)
    flow.handle("tap", position=P, now=0, trading=False, watch=True)
    assert flow.ui(0, trading=False)["message"] == "Watch only - not your account, nothing sent"


# --- end to end -----------------------------------------------------------------------------


@pytest.fixture
def risk_client(tmp_path: Path) -> Iterator[tuple[TestClient, MarketState]]:
    holder: list[MarketState] = []

    def make(state: MarketState) -> FakeAdapter:
        apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
        ctx = {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {"markPx": "3494.4"}}}
        apply_ws_message(state, ctx)
        holder.append(state)
        return FakeAdapter(state, [ETH_ROW])

    app = create_app(
        settings(tmp_path), run_feed=False, run_bot=False, heartbeat_s=0.2, make_adapter=make
    )
    with TestClient(app) as c:
        yield c, holder[0]


def mark(state: MarketState, px: str) -> None:
    apply_ws_message(
        state, {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {"markPx": px}}}
    )


def test_alert_reaches_connected_glasses_and_snooze_works(
    risk_client: tuple[TestClient, MarketState],
) -> None:
    client, state = risk_client
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        client.portal.call(mark, state, "3100")  # type: ignore[union-attr]  # liq 2980: 3.9 %
        alert = recv(ws, "hud", mode="menu")["ui"]
        assert alert["kind"] == "alert" and alert["alert"]["threshold_pct"] == 5.0
        for _ in range(3):
            ws.send_json({"type": "intent", "action": "next"})
            recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "tap"})  # Snooze 5m
        ack = recv(ws, "ack")
        assert ack["mode"] == "hud" and ack["note"] == "alert snoozed 5 min"
    status = client.get("/healthz").json()["alerts"]
    assert status["snoozed"] == ["ETH LONG"] and status["last"]["delivered_to"] == "glasses (1)"


def test_alert_goes_to_telegram_when_no_glasses(
    risk_client: tuple[TestClient, MarketState],
) -> None:
    client, state = risk_client
    notifier = Notifier()
    client.app.state.alerts.notifier = notifier  # type: ignore[attr-defined]
    client.portal.call(mark, state, "3050")  # type: ignore[union-attr]
    for _ in range(50):
        if notifier.sent:
            break
        time.sleep(0.05)
    assert notifier.sent and "ETH-PERP LONG" in notifier.sent[0]
    assert client.get("/healthz").json()["alerts"]["last"]["delivered_to"] == "telegram"
