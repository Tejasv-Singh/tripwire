"""WATCH mode: follow a public testnet address read-only (config, state, frame, flow, socket)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tripwire.actions import ActionFlow
from tripwire.config import Settings
from tripwire.hl.feed import apply_ws_message
from tripwire.hud import build_hud_frame
from tripwire.main import create_app
from tripwire.state import MarketState

from .test_state_feed import ch_msg

WATCHED = "0xb7bd4c71862bc7a2ba312694c641105330216422"
OWN = "0xabcdef0123456789abcdef0123456789abcdef01"
CODE = "test-pairing-code"


def make(tmp_path: Path, **kw: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, pairing_code=CODE, db_path=tmp_path / "w.sqlite3", **kw
    )


def test_watch_mode_needs_watch_address_and_no_own_account(tmp_path: Path) -> None:
    s = make(tmp_path, watch_address=WATCHED.upper().replace("0X", "0x"))
    assert s.watch_mode and s.stream_address == WATCHED  # lowercased
    own = make(tmp_path, watch_address=WATCHED, hl_account_address=OWN)
    assert not own.watch_mode and own.stream_address == OWN  # own account wins
    assert not make(tmp_path).watch_mode and make(tmp_path).stream_address == ""
    with pytest.raises(ValidationError):
        make(tmp_path, watch_address="0x123")


def test_state_mode_and_watched_positions() -> None:
    s = MarketState(account=WATCHED, watch=True)
    assert s.mode == "watch" and s.account_short == "0xb7bd...6422"
    apply_ws_message(s, ch_msg(user=WATCHED))
    assert [p.coin for p in s.positions] == ["ETH"]
    assert MarketState(account=OWN).mode == "account"
    assert MarketState().mode == "price"
    assert MarketState(watch=True).watch is False  # watch without an address means nothing


def test_frame_flags_watch() -> None:
    s = MarketState(account=WATCHED, watch=True)
    apply_ws_message(s, ch_msg(user=WATCHED))
    f = build_hud_frame(s, index=0, seq=1)
    assert f["watch"] is True and f["address"] == "0xb7bd...6422"
    assert f["position"]["coin"] == "ETH"


def test_tap_never_opens_the_menu_in_watch_mode() -> None:
    s = MarketState(account=WATCHED, watch=True)
    apply_ws_message(s, ch_msg(user=WATCHED))
    flow = ActionFlow()
    out = flow.handle("tap", position=s.positions[0], now=0, trading=True, watch=True)
    assert flow.mode == "hud" and not out.ok and out.note == "watch only"
    assert (
        flow.handle("next", position=s.positions[0], now=0, trading=True, watch=True).nav == "next"
    )


def test_app_in_watch_mode_is_read_only(tmp_path: Path) -> None:
    app = create_app(make(tmp_path, watch_address=WATCHED), run_feed=False, run_bot=False)
    with TestClient(app) as client:
        h = client.get("/healthz").json()
        assert (
            h["hyperliquid"]["mode"] == "watch" and h["hyperliquid"]["address"] == "0xb7bd...6422"
        )
        assert h["trading"]["status"] == "off" and "watch only" in h["trading"]["detail"]
        apply_ws_message(client.app.state.market, ch_msg(user=WATCHED))  # type: ignore[attr-defined]
        with client.websocket_connect(f"/ws?code={CODE}") as ws:
            ws.receive_json()  # hello
            for _ in range(10):
                hud = ws.receive_json()
                if hud["type"] == "hud" and hud["position"]:
                    break
            assert hud["watch"] is True and hud["position"]["coin"] == "ETH"
            ws.send_json({"type": "intent", "action": "tap"})
            for _ in range(10):
                ack = ws.receive_json()
                if ack["type"] == "ack":
                    break
            assert ack["ok"] is False and ack["mode"] == "hud" and ack["note"] == "watch only"
