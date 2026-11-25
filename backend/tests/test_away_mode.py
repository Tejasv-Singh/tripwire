"""Away mode (Day 4): plugins have no background path to the glasses, so an alert that fires
while nobody wears them goes to Telegram once per episode, and the glasses open straight on the
RISK screen when they reconnect, until the alert is acted on, snoozed or resolved."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tripwire.drafts import parse_draft
from tripwire.exchanges.fake import FakeAdapter
from tripwire.hl.feed import apply_ws_message
from tripwire.hl.positions import Position
from tripwire.main import create_app
from tripwire.policy import Decision
from tripwire.risk import DM_MIN_GAP_S, Alert, AlertService, RiskEngine
from tripwire.state import MarketState

from .test_app import CODE, ETH_ROW, recv, settings

P = Position(
    coin="ETH", side="LONG", size=0.5, entry=1700.0, mark=1500.0, upnl=-100.0, upnl_pct=-11.8,
    roe_pct=-58.8, liq_px=1420.0, liq_dist_pct=5.33, leverage=5.0, mid=1500.0,
    mark_source="markPx", leverage_type="cross",
)  # fmt: skip


def at(dist: float, **kw: Any) -> Position:
    return dataclasses.replace(P, liq_dist_pct=dist, **kw)


class Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_owner(self, text: str) -> bool:
        self.sent.append(text)
        return True


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def service(n: Notifier, clock: Clock, glasses: list[int] | None = None) -> AlertService:
    shown = glasses if glasses is not None else [0]
    return AlertService(
        RiskEngine(threshold_pct=5.0, hysteresis_pct=1.0),
        FakeAdapter(MarketState()),
        glasses=lambda _a: shown[0],
        notifier=n,
        clock=clock,
    )


# --- the DM ----------------------------------------------------------------------------------


def test_away_dm_is_a_one_line_summary_plus_the_instruction() -> None:
    text = Alert.from_position(at(4.2, mark=1482.0, upnl=-109.0), 5.0).text()
    assert text.splitlines() == [
        "▲ RISK - Away mode",
        "ETH-PERP LONG 0.5 @ 1,700 5x | uPnL -$109.00 | liq 1,420: 4.2% from mark 1,482 "
        "(alert at 5%)",
        "Open Tripwire on your glasses to act.",
    ]


# --- one DM per episode ----------------------------------------------------------------------


async def test_alert_while_away_dms_once_per_episode() -> None:
    n, clock = Notifier(), Clock()
    svc = service(n, clock)
    assert await svc.check([at(4.0)]) and n.sent  # fires -> away -> DM
    for d in (3.5, 3.0, 2.0):  # deeper inside: same episode, the engine stays quiet
        clock.t += 30
        assert await svc.check([at(d)]) == []
    svc.set_threshold(5.0)  # /threshold re-arms -> the open episode fires again ...
    clock.t += 30
    (again,) = await svc.check([at(2.0)])
    assert svc.last_delivery is not None
    assert svc.last_delivery["delivered_to"] == "telegram (already sent for this alert)"
    assert len(n.sent) == 1  # ... but no second DM
    (ep,) = svc.status()["open"]
    assert ep["position"] == "ETH LONG" and ep["dm_sent"] is True and ep["liq_dist_pct"] == 2.0
    assert svc.status()["dms_sent"] == 1 and svc.status()["dms_suppressed"] == 1


async def test_wobbling_around_the_clear_line_cannot_spam() -> None:
    """Recovered (episode closed) and back inside a minute later: a new episode, but the
    per-position gap holds the DM back until DM_MIN_GAP_S has passed."""
    n, clock = Notifier(), Clock()
    svc = service(n, clock)
    await svc.check([at(4.0)])
    clock.t = 60
    await svc.check([at(6.5)])  # >= 5 + 1: resolved, engine re-armed
    assert svc.episodes == {}
    clock.t = 120
    await svc.check([at(4.5)])
    assert len(n.sent) == 1 and svc.last_delivery is not None
    assert svc.last_delivery["delivered_to"].startswith("telegram suppressed (last DM for ETH")
    clock.t = 120 + DM_MIN_GAP_S
    await svc.check([at(6.5)])
    clock.t += 1
    await svc.check([at(4.5)])
    assert len(n.sent) == 2  # a real new episode after the gap is DMed


async def test_episode_closes_on_snooze_act_resolve_or_position_gone() -> None:
    n, clock = Notifier(), Clock()
    svc = service(n, clock)
    await svc.check([at(4.0)])
    assert svc.close_episode("ETH", "LONG", "snoozed") is True
    assert svc.close_episode("ETH", "LONG", "snoozed") is False  # already closed
    svc.engine.snooze("ETH", "LONG", clock.t)
    clock.t = 301  # snooze over, still inside -> a new episode
    await svc.check([at(3.0)])
    assert list(svc.episodes) == [("ETH", "LONG")]
    await svc.check([])  # position closed
    assert svc.episodes == {}


async def test_shown_on_glasses_means_no_dm_but_the_episode_stays_open() -> None:
    n, clock = Notifier(), Clock()
    svc = service(n, clock, glasses=[1])
    await svc.check([at(4.0)])
    assert n.sent == [] and svc.episodes[("ETH", "LONG")].shown_on_glasses


async def test_active_alert_is_the_most_urgent_with_live_numbers() -> None:
    n, clock = Notifier(), Clock()
    svc = service(n, clock)
    btc = at(2.0, coin="BTC", side="SHORT", liq_px=70_000.0, mark=68_600.0)
    await svc.check([at(4.0), btc])
    assert len(n.sent) == 2  # one per position
    await svc.check([at(1.5), dataclasses.replace(btc, liq_dist_pct=2.5)])  # no re-fire
    active = svc.active_alert()
    assert active is not None and (active.coin, active.liq_dist_pct) == ("ETH", 1.5)  # fresh
    assert service(Notifier(), Clock()).active_alert() is None


# --- end to end: away -> DM once -> reconnect -> RISK first -> act -> closed --------------------


@pytest.fixture
def away_client(tmp_path: Path) -> Iterator[tuple[TestClient, MarketState, Notifier]]:
    holder: list[MarketState] = []

    def make(state: MarketState) -> FakeAdapter:
        apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
        mark(state, "3494.4")
        holder.append(state)
        return FakeAdapter(state, [ETH_ROW])  # 0.5 ETH long, liq 2980: 14.7 % away

    app = create_app(
        settings(tmp_path), run_feed=False, run_bot=False, heartbeat_s=0.2, make_adapter=make
    )
    notifier = Notifier()
    app.state.alerts.notifier = notifier
    with TestClient(app) as c:
        yield c, holder[0], notifier


def mark(state: MarketState, px: str) -> None:
    apply_ws_message(
        state, {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {"markPx": px}}}
    )


def wait_for(cond: Any, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_away_dm_once_then_reconnect_lands_on_risk_then_acting_closes_it(
    away_client: tuple[TestClient, MarketState, Notifier],
) -> None:
    client, state, notifier = away_client
    alerts = client.app.state.alerts  # type: ignore[attr-defined]
    portal = client.portal  # type: ignore[union-attr]
    # 1. nobody wears the glasses: the alert fires -> exactly one DM
    portal.call(mark, state, "3050")  # liq 2980: 2.3 %
    wait_for(lambda: len(notifier.sent) == 1)
    assert notifier.sent[0].startswith("▲ RISK - Away mode\nETH-PERP LONG 0.5 @ 3,412 5x")
    assert notifier.sent[0].endswith("Open Tripwire on your glasses to act.")
    portal.call(mark, state, "3030")
    portal.call(alerts.set_threshold, 5.0)  # re-arm: the open episode fires again...
    portal.call(mark, state, "3020")
    wait_for(lambda: alerts.engine.fired == 2)
    assert len(notifier.sent) == 1  # ...but there is still only one DM
    health = client.get("/healthz").json()["alerts"]
    assert health["away_mode"] is True and health["dms_sent"] == 1
    assert [e["position"] for e in health["open"]] == ["ETH LONG"]
    assert "Away mode: ON" in client.app.state.bot_services.status_text()  # type: ignore[attr-defined]

    # 2. the glasses come back: the first screen is the RISK alert, with the live distance
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        assert ws.receive_json()["type"] == "hello"
        first = recv(ws, "hud")
        ui = first["ui"]
        assert ui["mode"] == "menu" and ui["kind"] == "alert"
        assert ui["items"] == ["Move SL", "Close 50%", "Close all", "Snooze 5m"]
        assert ui["alert"]["liq_dist_pct"] == pytest.approx((3020 - 2980) / 3020 * 100, abs=0.01)
        assert client.get("/healthz").json()["alerts"]["away_mode"] is False
        # 3. act on it: Close 50% with the two-tap confirm
        ws.send_json({"type": "intent", "action": "next"})
        recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "tap"})
        recv(ws, "hud", mode="confirm")
        time.sleep(0.35)
        ws.send_json({"type": "intent", "action": "tap"})
        assert recv(ws, "order")["status"] == "filled"
    assert alerts.episodes == {}  # acted on -> closed

    # 4. reconnecting now opens on the HUD
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.receive_json()  # hello
        assert recv(ws, "hud")["ui"] == {"mode": "hud"}
    assert len(notifier.sent) == 1


def test_snooze_ends_it_and_a_waiting_draft_comes_after_the_alert(
    away_client: tuple[TestClient, MarketState, Notifier],
) -> None:
    client, state, notifier = away_client
    hub = client.app.state.hub  # type: ignore[attr-defined]
    portal = client.portal  # type: ignore[union-attr]
    portal.call(mark, state, "3050")
    wait_for(lambda: len(notifier.sent) == 1)
    # a forwarded draft waiting for the next client (one-shot, like ClientHub.pending_draft)
    pending = [(parse_draft("BTC long 96.5k sl 95k tp 99k"), Decision(True, "open", "ok"))]
    hub.take_pending_draft = lambda _now: pending.pop() if pending else None
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.receive_json()  # hello
        assert recv(ws, "hud")["ui"]["kind"] == "alert"  # the alert first ...
        for _ in range(3):
            ws.send_json({"type": "intent", "action": "next"})
            recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "tap"})  # Snooze 5m
        ack = recv(ws, "ack")
        assert ack["note"] == "alert snoozed 5 min"
        assert recv(ws, "hud", mode="menu")["ui"]["kind"] == "draft"  # ... then the draft
    assert client.app.state.alerts.episodes == {}  # type: ignore[attr-defined]
    with client.websocket_connect(f"/ws?code={CODE}") as ws:  # snoozed: no RISK screen now
        ws.receive_json()
        assert recv(ws, "hud")["ui"] == {"mode": "hud"}


def test_only_a_real_change_closes_the_episode(tmp_path: Path) -> None:
    """A dry run, a block or a rejection leaves the risk (and the alert) in place; a fill or a
    placed stop on that position closes it; an open order elsewhere doesn't."""
    from tripwire.orders import CloseOrder, OrderResult, StopOrder
    from tripwire.risk import Episode

    app = create_app(settings(tmp_path), run_feed=False, run_bot=False)
    alerts, hook = app.state.alerts, app.state.on_order_result
    close = CloseOrder("ETH", "LONG", 0.5, 0.25)
    stop = StopOrder("ETH", "LONG", 1600.0, None, None, "SL to breakeven")
    cases = [
        (close, OrderResult(True, "dry_run", "DRY RUN: signed, not sent"), False),
        (close, OrderResult(False, "blocked", "Kill switch ON"), False),
        (close, OrderResult(False, "rejected", "no liquidity"), False),
        (CloseOrder("BTC", "LONG", 0.5, 0.1), OrderResult(True, "filled", "x"), False),
        (close, OrderResult(True, "filled", "Filled 0.25 @ 3,000"), True),
        (stop, OrderResult(True, "placed", "SL set at 1600"), True),
    ]
    for order, result, closes in cases:
        alerts.episodes[("ETH", "LONG")] = Episode(Alert.from_position(at(4.0), 5.0), 0.0)
        hook(order, result)
        assert (("ETH", "LONG") not in alerts.episodes) is closes, (order, result.status)
