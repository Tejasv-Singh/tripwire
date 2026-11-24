"""Day 3 robustness: exchange event -> glasses render latency, 1 s heartbeat, glyphs flag."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tripwire.config import Settings
from tripwire.hl.feed import apply_ws_message
from tripwire.latency import MAX_PLAUSIBLE_MS, LatencyStats
from tripwire.main import create_app
from tripwire.state import MarketState

CODE = "test-pairing-code"


def settings(tmp_path: Path, **kw: object) -> Settings:
    kw.setdefault("dry_run", False)
    kw.setdefault("voice_enabled", False)
    return Settings(  # type: ignore[call-arg]
        _env_file=None, pairing_code=CODE, db_path=tmp_path / "t.sqlite3", **kw
    )


# --- LatencyStats ---------------------------------------------------------------------------------


def test_empty_stats_say_so() -> None:
    s = LatencyStats()
    assert s.summary() is None
    assert s.line() == "Latency (HL event -> glasses render): no samples yet (connect the glasses)"


def test_percentiles_over_samples() -> None:
    s = LatencyStats()
    for ms in range(1, 101):  # 1..100 ms
        assert s.add_ack(10_000, now_ms=10_000 + ms) == ms
    assert s.summary() == {"n": 100, "p50_ms": 51, "p95_ms": 95, "max_ms": 100}
    assert s.line() == "Latency (HL event -> glasses render): p50 51 ms, p95 95 ms (n=100)"


def test_implausible_acks_are_ignored() -> None:
    s = LatencyStats()
    assert s.add_ack(10_000, now_ms=9_000) is None  # from the future (clock jump)
    assert s.add_ack(0, now_ms=MAX_PLAUSIBLE_MS + 1) is None  # minutes old: not a render
    assert s.summary() is None
    assert s.add_ack(0, now_ms=MAX_PLAUSIBLE_MS) == MAX_PLAUSIBLE_MS


def test_window_keeps_the_newest_samples() -> None:
    s = LatencyStats(window=3)
    for ms in (900, 900, 900, 10, 20, 30):
        s.add_ack(0, now_ms=ms)
    assert s.summary() == {"n": 3, "p50_ms": 20, "p95_ms": 30, "max_ms": 30}


# --- end to end over the WebSocket ---------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(settings(tmp_path, glyphs="ascii"), run_feed=False, run_bot=False)
    with TestClient(app) as c:
        yield c


def hud(ws: Any, limit: int = 20) -> dict[str, Any]:
    for _ in range(limit):
        msg: dict[str, Any] = ws.receive_json()
        if msg["type"] == "hud":
            return msg
    raise AssertionError("no hud frame")


def test_frame_carries_src_ms_and_glyphs_and_ack_feeds_status(client: TestClient) -> None:
    state: MarketState = client.app.state.market  # type: ignore[attr-defined]
    before = int(time.time() * 1000)
    apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "1721.25"}}})
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["glyphs"] == "ascii"
        assert hello["heartbeat_ms"] == 1000  # the glasses flag STALE after 3 s without a frame
        frame = hud(ws)
        assert frame["glyphs"] == "ascii"
        src = frame["src_ms"]
        assert isinstance(src, int) and before <= src <= int(time.time() * 1000)
        ws.send_json({"type": "rendered", "seq": frame["seq"], "src_ms": src})
        ws.send_json({"type": "rendered", "seq": frame["seq"], "src_ms": src - 120_000})  # ignored
        ws.send_json({"type": "intent", "action": "next"})  # round trip: acks were processed
        while ws.receive_json()["type"] != "ack":
            pass
    stats = client.app.state.latency.summary()  # type: ignore[attr-defined]
    assert stats is not None and stats["n"] == 1 and 0 <= stats["p50_ms"] < 10_000
    assert client.get("/healthz").json()["latency"]["n"] == 1


def test_bad_rendered_message_is_an_error_not_a_crash(client: TestClient) -> None:
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "rendered", "seq": 1})  # no src_ms
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "error":
                assert msg["error"] == "bad_message"
                break
        else:
            raise AssertionError("no error frame")
        ws.send_json({"type": "intent", "action": "next"})  # still alive
        while ws.receive_json()["type"] != "ack":
            pass


def test_status_text_ends_with_the_latency_line(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), run_feed=False, run_bot=False)
    with TestClient(app):
        app.state.latency.add_ack(1_000, now_ms=1_180)
        text = app.state.bot_services.status_text()
    assert (
        text.splitlines()[-1]
        == "Latency (HL event -> glasses render): p50 180 ms, p95 180 ms (n=1)"
    )


def test_unicode_is_the_default_glyph_set(tmp_path: Path) -> None:
    assert settings(tmp_path).glyphs == "unicode"
    with pytest.raises(ValueError):
        settings(tmp_path, glyphs="emoji")
