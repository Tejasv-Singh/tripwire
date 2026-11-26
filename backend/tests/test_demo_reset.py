"""npm run demo:reset (Day 5 wrap-up): a clean slate between demo takes, behind the pairing code,
never through the tunnel, never touching .env, the exchange or the kill switch."""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tripwire.exchanges.fake import FakeAdapter
from tripwire.hl.feed import apply_ws_message
from tripwire.main import create_app
from tripwire.orders import CloseOrder, OrderResult
from tripwire.risk import THRESHOLD_KEY
from tripwire.state import MarketState

from .test_app import CODE, ETH_ROW, recv, settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo_reset.py"


class Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_owner(self, text: str) -> bool:
        self.sent.append(text)
        return True


def mark(state: MarketState, px: str) -> None:
    apply_ws_message(
        state, {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {"markPx": px}}}
    )


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[tuple[TestClient, MarketState, FakeAdapter, Notifier]]:
    holder: list[Any] = []

    def make(state: MarketState) -> FakeAdapter:
        apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
        mark(state, "3494.4")
        fake = FakeAdapter(state, [ETH_ROW])  # 0.5 ETH long, liq 2980
        holder.extend([state, fake])
        return fake

    app = create_app(
        settings(tmp_path), run_feed=False, run_bot=False, heartbeat_s=0.2, make_adapter=make
    )
    notifier = Notifier()
    app.state.alerts.notifier = notifier
    with TestClient(app) as c:
        yield c, holder[0], holder[1], notifier


def wait_for(cond: Any, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_reset_is_guarded(
    app_client: tuple[TestClient, MarketState, FakeAdapter, Notifier],
) -> None:
    client = app_client[0]
    assert client.post("/admin/demo-reset").status_code == 401
    assert client.post("/admin/demo-reset", headers={"X-Pairing-Code": "wrong"}).status_code == 401
    via_tunnel = {"X-Pairing-Code": CODE, "X-Forwarded-For": "203.0.113.7"}  # what ngrok adds
    assert client.post("/admin/demo-reset", headers=via_tunnel).status_code == 403
    assert client.get("/admin/demo-reset").status_code == 405  # POST only


def test_reset_clears_alerts_cooldowns_threshold_session_and_screens(
    app_client: tuple[TestClient, MarketState, FakeAdapter, Notifier],
) -> None:
    client, state, fake, notifier = app_client
    app: Any = client.app
    alerts, sessions, portal = app.state.alerts, app.state.sessions, client.portal
    # a take: threshold raised, alert while away (DM + cooldown), an order, a glasses client
    portal.call(alerts.set_threshold, 20.0)
    portal.call(mark, state, "3300")  # liq 2980: 9.7 % < 20 %
    wait_for(lambda: len(notifier.sent) == 1)
    sessions.tracker.record_order(
        CloseOrder("ETH", "LONG", 0.5, 0.25), OrderResult(True, "filled", "Filled 0.25 @ 3,300")
    )
    portal.call(mark, state, "3494.4")  # back out (14.7 % > default 5 % + 1): nothing re-fires
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.receive_json()  # hello
        assert recv(ws, "hud")["ui"]["kind"] == "alert"  # the open episode opens first
        resp = client.post("/admin/demo-reset", headers={"X-Pairing-Code": CODE})
        assert resp.status_code == 200
        body = resp.json()
        assert body["reset"]["alerts"] == {"episodes": 1, "cooldowns": 1, "threshold_pct": 5.0}
        assert body["reset"]["clients_back_to_hud"] == 1
        assert ".env" in body["untouched"] and "kill switch" in body["untouched"]
        assert recv(ws, "hud", mode="hud")["ui"] == {"mode": "hud"}  # back on the plain HUD
    assert alerts.episodes == {} and alerts._last_dm == {}
    status = alerts.status(0.0)  # explicit time: this test thread has no event loop
    assert status["fired"] == 0 and status["inside"] == [] and status["dms_sent"] == 0
    assert alerts.engine.threshold_pct == 5.0
    assert app.state.store.kv_get(THRESHOLD_KEY) == "5"  # the saved /threshold is reset too
    snap = sessions.snapshot()
    assert snap.trades == 0 and snap.alerts_fired == 0 and snap.session_pnl == pytest.approx(0.0)
    assert app.state.store.recent_sessions() == []  # the aborted take is not stored
    assert app.state.latency.summary() is None
    assert fake.state.positions and fake.calls == []  # the exchange was not touched
    # a new take can DM again right away (the cooldown is gone)
    portal.call(alerts.set_threshold, 20.0)
    portal.call(mark, state, "3300")
    wait_for(lambda: len(notifier.sent) == 2)


def test_script_resets_the_saved_threshold_when_the_backend_is_down(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("demo_reset", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    s = settings(tmp_path, alert_liq_pct=5.0)
    from tripwire.db import Store

    store = Store(s.resolved_db_path)
    store.init()
    store.kv_set(THRESHOLD_KEY, "25")
    assert mod.reset_offline(s) == "saved alert threshold 25 -> 5%"
    assert store.kv_get(THRESHOLD_KEY) == "5"
