"""App wiring: /healthz, CORS, /ws pairing, frames, intents, throttle â€” feed and bot disabled."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hyperliquid.utils import constants
from starlette.websockets import WebSocketDisconnect

from tripwire.config import Settings, TestnetGuardError
from tripwire.hl.feed import apply_ws_message
from tripwire.main import RedactSecretsFilter, create_app, redact
from tripwire.state import MarketState
from tripwire.telegram_bot import TelegramBot, build_application
from tripwire.trading import CloseRequest, OrderResult, TradingStatus

from .test_state_feed import ACCOUNT, ch_msg

CODE = "test-pairing-code"


def settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, pairing_code=CODE, db_path=tmp_path / "t.sqlite3", **kw
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(settings(tmp_path), run_feed=False, run_bot=False, heartbeat_s=0.2)
    with TestClient(app) as c:
        yield c


def test_refuses_to_start_on_mainnet(tmp_path: Path) -> None:
    with pytest.raises(TestnetGuardError):
        create_app(settings(tmp_path, hl_api_url=constants.MAINNET_API_URL), run_feed=False)


def test_healthz(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["ok"] is True and body["network"] == "testnet"
    assert body["hyperliquid"]["connected"] is False
    assert body["telegram"]["status"] == "disabled"
    assert body["trading"]["enabled"] is False and body["trading"]["status"] == "off"
    assert body["ws_clients"] == 0


def test_cors_allows_plugin_dev_origin_and_ngrok_header(client: TestClient) -> None:
    r = client.options(
        "/healthz",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "ngrok-skip-browser-warning",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    r = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_ws_rejects_bad_pairing_code(client: TestClient) -> None:
    with client.websocket_connect("/ws?code=wrong") as ws, pytest.raises(WebSocketDisconnect) as e:
        ws.receive_json()
    assert e.value.code == 4401


def test_ws_hello_then_hud_frame_then_intents(client: TestClient) -> None:
    state: MarketState = client.app.state.market  # type: ignore[attr-defined]
    apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "1721.25"}}})
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["min_interval_ms"] == 500
        assert hello["v"] == 2 and hello["trading"] == {"enabled": False, "status": "off"}
        hud = ws.receive_json()
        assert hud["type"] == "hud" and hud["ref"]["mid"] == 1721.25 and hud["position"] is None
        assert hud["ui"] == {"mode": "hud"}

        ws.send_json({"type": "intent", "action": "next"})
        ack = recv(ws, "ack")
        assert ack == {
            "type": "ack",
            "action": "next",
            "ok": True,
            "mode": "hud",
            "view": {"index": 0, "count": 0},
        }

        ws.send_json({"type": "intent", "action": "tap"})  # no position -> no menu
        ack = recv(ws, "ack")
        assert ack["ok"] is False and ack["mode"] == "hud" and ack["note"] == "no open position"

        for bad in (
            '{"type":"intent","action":"sell_everything"}',
            '{"type":"intent","action":"actions"}',
        ):  # v1 name is gone in v2
            ws.send_text(bad)
            err = recv(ws, "error")
            assert err["error"] == "bad_message"

    store = client.app.state.store  # type: ignore[attr-defined]
    assert [a for _, a, _ in store.recent_intents()] == ["tap", "next"]


def recv(ws: Any, kind: str, *, mode: str | None = None, limit: int = 40) -> dict[str, Any]:
    """Next frame of `kind` (skipping hud frames / heartbeats in between)."""
    for _ in range(limit):
        msg: dict[str, Any] = ws.receive_json()
        if msg["type"] == kind and (mode is None or msg.get("ui", {}).get("mode") == mode):
            return msg
    raise AssertionError(f"no {kind} frame (mode={mode}) within {limit} frames")


class FakeExecutor:
    def __init__(self) -> None:
        self.status: TradingStatus = "ready"
        self.detail = "fake"
        self.requests: list[CloseRequest] = []

    def size_decimals(self, coin: str) -> int | None:
        return 4

    async def warm_up(self) -> None:
        return None

    async def close(self, req: CloseRequest) -> OrderResult:
        self.requests.append(req)
        return OrderResult(True, "filled", "Filled 0.25 @ 3499.1", 0.25, 3499.1, 77)


@pytest.fixture
def trading_client(tmp_path: Path) -> Iterator[tuple[TestClient, FakeExecutor]]:
    fake = FakeExecutor()
    app = create_app(
        settings(tmp_path, hl_account_address=ACCOUNT),
        run_feed=False,
        run_bot=False,
        heartbeat_s=0.2,
        executor=fake,
    )
    with TestClient(app) as c:
        state: MarketState = c.app.state.market  # type: ignore[attr-defined]
        apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
        apply_ws_message(state, ch_msg())
        yield c, fake


def test_two_tap_close_end_to_end(trading_client: tuple[TestClient, FakeExecutor]) -> None:
    client, fake = trading_client
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        assert ws.receive_json()["trading"] == {"enabled": True, "status": "ready"}
        hud = recv(ws, "hud")
        assert hud["position"]["coin"] == "ETH" and hud["position"]["roe_pct"] is not None

        ws.send_json({"type": "intent", "action": "tap"})  # open the menu
        assert recv(ws, "ack")["mode"] == "menu"
        menu = recv(ws, "hud", mode="menu")["ui"]
        assert menu["items"][0] == "Close 25%" and menu["cursor"] == 0 and menu["trading"] is True

        ws.send_json({"type": "intent", "action": "next"})  # cursor -> Close 50%
        recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "tap"})  # tap 1: arm only
        assert recv(ws, "ack")["mode"] == "confirm"
        confirm = recv(ws, "hud", mode="confirm")["ui"]
        assert (confirm["label"], confirm["size"], confirm["side"]) == ("Close 50%", 0.25, "LONG")
        assert fake.requests == []  # nothing sent on the first tap

        time.sleep(0.35)  # > MIN_CONFIRM_GAP_S
        ws.send_json({"type": "intent", "action": "tap"})  # tap 2: send
        order = recv(ws, "order")
        assert order["ok"] is True and order["status"] == "filled" and order["coin"] == "ETH"
        result = recv(ws, "hud", mode="result")["ui"]
        assert result["message"] == "Filled 0.25 @ 3499.1"

    assert fake.requests == [CloseRequest("ETH", "LONG", 0.5, 0.25)]
    rows = client.app.state.store.recent_orders()  # type: ignore[attr-defined]
    assert len(rows) == 1 and rows[0]["status"] == "filled" and rows[0]["oid"] == 77


def test_confirm_window_expires_without_sending(
    trading_client: tuple[TestClient, FakeExecutor], monkeypatch: pytest.MonkeyPatch
) -> None:
    import tripwire.actions as actions

    monkeypatch.setattr(actions, "CONFIRM_WINDOW_S", 0.4)
    client, fake = trading_client
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.send_json({"type": "intent", "action": "tap"})
        recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "tap"})  # arm Close 25%
        assert recv(ws, "ack")["mode"] == "confirm"
        menu = recv(ws, "hud", mode="menu")  # timer fired -> back to the menu
        assert menu["ui"]["note"] == "Confirm expired - not sent"
    assert fake.requests == []


def test_ws_frames_are_throttled_to_500ms(client: TestClient) -> None:
    state: MarketState = client.app.state.market  # type: ignore[attr-defined]
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.receive_json()  # hello
        ws.receive_json()  # first hud (immediate)
        t0 = time.monotonic()
        for i in range(5):  # burst of changes -> must coalesce
            apply_ws_message(
                state, {"channel": "allMids", "data": {"mids": {"ETH": str(1700 + i)}}}
            )
        hud = ws.receive_json()
        elapsed = time.monotonic() - t0
    assert hud["type"] == "hud"
    assert elapsed >= 0.45  # min interval honoured (small slack for timer granularity)


def test_ws_heartbeat_frames_arrive_without_changes(client: TestClient) -> None:
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.receive_json()  # hello
        first = ws.receive_json()
        second = ws.receive_json()  # heartbeat_s=0.2 in the fixture (min interval still 0.5)
    assert second["seq"] == first["seq"] + 1


def test_log_redaction_hides_pairing_code_and_bot_token() -> None:
    line = '127.0.0.1:1 - "WebSocket /ws?code=abc123XYZ&x=1" [accepted]'
    assert redact(line) == '127.0.0.1:1 - "WebSocket /ws?code=***&x=1" [accepted]'
    url = "POST https://api.telegram.org/bot123456:AAE-xyz_987/getUpdates"
    assert redact(url) == "POST https://api.telegram.org/bot***/getUpdates"


def test_redact_filter_rewrites_records(caplog: pytest.LogCaptureFixture) -> None:
    lg = logging.getLogger("uvicorn.error")
    lg.addFilter(RedactSecretsFilter())
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        lg.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:1", f"/ws?code={CODE}")
    assert CODE not in caplog.text and "code=***" in caplog.text


async def test_bot_with_empty_token_is_disabled() -> None:
    bot = TelegramBot("", store=None)
    await bot.start()
    assert bot.status == "disabled"
    await bot.stop()


def test_bot_application_registers_handlers_offline() -> None:
    app = build_application("123456:TEST-TOKEN-not-real", store=None)
    assert len(app.handlers[0]) == 6  # start, kill, unkill, business conn/msg, forward
