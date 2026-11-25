"""Push-to-talk (Day 3, item 2): engine guards, ActionFlow.voice(), and /ws end to end with a
fake transcriber (the real faster-whisper model is exercised by the simulator exit check)."""

from __future__ import annotations

import dataclasses
import math
import struct
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tripwire.actions import ActionFlow, ReplyOffer
from tripwire.exchanges.base import StopInfo
from tripwire.exchanges.fake import FakeAdapter
from tripwire.hl.feed import apply_ws_message
from tripwire.hl.positions import Position
from tripwire.inbox import ReplyTarget
from tripwire.main import create_app
from tripwire.orders import OrderResult, StopOrder
from tripwire.risk import Alert
from tripwire.state import MarketState
from tripwire.voice import BYTES_PER_SECOND, MAX_SECONDS, VoiceEngine, is_hallucination, pcm_rms
from tripwire.voice_grammar import VoiceCommand, parse_voice

from .test_app import CODE, ETH_ROW, recv, settings

P = Position(
    coin="ETH", side="LONG", size=0.5, entry=1700.0, mark=1750.0, upnl=25.0, upnl_pct=2.94,
    roe_pct=14.7, liq_px=1420.0, liq_dist_pct=18.9, leverage=5.0, mid=1750.0,
    mark_source="markPx", leverage_type="cross",
)  # fmt: skip
HALF = VoiceCommand("close", 0.5)


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * BYTES_PER_SECOND))


def tone(seconds: float, amp: int = 8000) -> bytes:
    n = int(seconds * 16_000)
    return b"".join(struct.pack("<h", int(amp * math.sin(i / 8))) for i in range(n))


def ctx(**kw: Any) -> dict[str, Any]:
    base = {"position": P, "now": 1.0, "trading": True, "size_decimals": 4}
    base.update(kw)
    return base


# --- engine --------------------------------------------------------------------------------------


async def test_engine_guards() -> None:
    calls: list[int] = []
    eng = VoiceEngine(transcriber=lambda pcm: calls.append(len(pcm)) or "close half")
    assert eng.status == "ready"
    assert await eng.transcribe(silence(0.1)) == "" and calls == []  # too short: model not run
    assert await eng.transcribe(tone(1.0)) == "close half"
    big = await eng.transcribe(tone(MAX_SECONDS + 3))
    assert big == "close half" and calls[-1] == int(MAX_SECONDS * BYTES_PER_SECOND)  # cut
    assert pcm_rms(silence(1)) == 0.0 and pcm_rms(tone(0.5)) > 0.1
    assert (
        is_hallucination(" Thank you. ")
        and is_hallucination("you")
        and not is_hallucination("close")
    )
    halluc = VoiceEngine(transcriber=lambda pcm: "Thank you.")
    assert await halluc.transcribe(tone(1.0)) == ""


async def test_engine_off_refuses() -> None:
    off = VoiceEngine(enabled=False)
    assert off.status == "off"
    with pytest.raises(RuntimeError, match="voice is off"):
        await off.transcribe(tone(1.0))


# --- flow -----------------------------------------------------------------------------------------


def test_voice_close_half_arms_the_same_confirm_as_the_menu() -> None:
    flow = ActionFlow()
    out = flow.voice(HALF, "close half", **ctx())
    assert flow.mode == "confirm" and out.execute is None and out.timer_s  # armed, NOT sent
    ui = flow.ui(1.0, trading=True)
    assert (ui["action"], ui["label"], ui["size"]) == ("close", "Close 50%", 0.25)
    sent = flow.handle("tap", position=P, now=1.5, trading=True)
    assert sent.execute is not None and sent.execute.fraction == 0.5  # the second tap sends


def test_voice_in_watch_mode_is_blocked_watch_only() -> None:
    flow = ActionFlow()
    out = flow.voice(HALF, "close half", **ctx(watch=True, trading=False))
    ui = flow.ui(1.0, trading=False)
    assert flow.mode == "result" and out.execute is None
    assert (ui["status"], ui["message"]) == (
        "blocked",
        "Watch only - not your account, nothing sent",
    )


def test_voice_goes_through_the_policy_precheck() -> None:
    from tripwire.policy import Decision

    flow = ActionFlow()
    flow.voice(HALF, "close half", **ctx(precheck=lambda o: Decision(False, "x", "nope")))
    assert flow.ui(1, trading=True)["message"] == "Blocked: nope"


def test_cancel_wins_and_busy_screens_refuse_new_commands() -> None:
    flow = ActionFlow()
    flow.voice(HALF, "close half", **ctx())
    assert flow.voice(VoiceCommand("close", 1.0), "close all", **ctx()).note == (
        "finish or cancel the current order first"
    )
    out = flow.voice(VoiceCommand("cancel"), "cancel", **ctx())
    assert flow.mode == "hud" and out.note == "cancelled"
    assert flow.voice(VoiceCommand("cancel"), "cancel", **ctx()).note == "nothing to cancel"


def test_status_snooze_and_no_position() -> None:
    flow = ActionFlow()
    assert flow.voice(VoiceCommand("status"), "status", **ctx()).show_status
    assert flow.voice(VoiceCommand("snooze"), "snooze", **ctx()).note == "no alert to snooze"
    flow.raise_alert(Alert("ETH", "LONG", 0.5, 1420, 4.1, 1480, 5.0))
    out = flow.voice(VoiceCommand("snooze"), "snooze", **ctx())
    assert out.snooze == ("ETH", "LONG") and flow.mode == "hud"
    assert flow.voice(HALF, "close half", **ctx(position=None)).note == "no open position"


def test_voice_stop_to_breakeven_waits_for_the_stop_lookup() -> None:
    flow = ActionFlow()
    out = flow.voice(VoiceCommand("sl_breakeven"), "stop to breakeven", **ctx())
    assert out.fetch_stop == "ETH" and flow.kind == "sl" and flow.mode == "menu"
    flow.set_stop("ETH", StopInfo("ETH", 11, 1600.0))
    key = flow.take_pending_voice()
    assert key == "sl_breakeven"
    flow.select_key(key, **ctx())
    assert flow.mode == "confirm"
    sent = flow.handle("tap", position=P, now=1.5, trading=True)
    assert sent.execute == StopOrder("ETH", "LONG", 1700.0, 1600.0, 11, "SL to breakeven")


def test_voice_stop_tighter_when_the_stop_is_already_loaded() -> None:
    flow = ActionFlow()
    flow.voice(VoiceCommand("sl_breakeven"), "x", **ctx())
    flow.set_stop("ETH", StopInfo("ETH", 11, 1600.0))
    flow.take_pending_voice()
    flow.handle("back", position=P, now=1, trading=True)  # leave, then speak again
    flow.voice(VoiceCommand("sl_tighten"), "stop tighter", **ctx())  # needs a fresh lookup
    flow.set_stop("ETH", StopInfo("ETH", 11, 1600.0))
    flow.select_key(flow.take_pending_voice() or "", **ctx())
    assert flow.ui(1, trading=True)["new_sl"] == 1617.5


def test_voice_replaces_the_reply_draft_and_cancel_skips_it() -> None:
    flow = ActionFlow()
    token = flow.finish(OrderResult(True, "filled", "Filled")).token
    flow.offer_reply(
        ReplyOffer(ReplyTarget("bc", 1, "@alice", 0), "Trimmed half.", "template"), token
    )
    parsed = parse_voice("out of half, rest runs with a stop")
    out = flow.voice(parsed.command, parsed.heard, **ctx())
    ui = flow.ui(1, trading=True)
    assert flow.mode == "reply" and ui["text"] == "out of half, rest runs with a stop"
    assert ui["source"] == "voice" and "tap to send" in (out.note or "")
    flow.voice(VoiceCommand("cancel"), "cancel", **ctx())
    assert flow.mode == "hud"


# --- /ws end to end -----------------------------------------------------------------------------


def make_client(tmp_path: Path, transcript: str, **kw: Any) -> Iterator[tuple[TestClient, Any]]:
    holder: list[Any] = []

    def make(state: MarketState) -> FakeAdapter:
        apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
        holder.append(FakeAdapter(state, [ETH_ROW]))
        return holder[0]

    engine = VoiceEngine(transcriber=lambda pcm: transcript)
    app = create_app(
        settings(tmp_path, **kw), run_feed=False, run_bot=False, heartbeat_s=0.2,
        make_adapter=None if kw.get("watch_address") else make, voice_engine=engine,
    )  # fmt: skip
    with TestClient(app) as c:
        yield c, holder[0] if holder else None


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[tuple[TestClient, Any]]:
    yield from make_client(tmp_path, "Close half.")


def push_to_talk(ws: Any, seconds: float = 1.0) -> dict[str, Any]:
    ws.send_json({"type": "voice", "action": "start"})
    assert recv(ws, "voice")["state"] == "listening"
    pcm = tone(seconds)
    for i in range(0, len(pcm), 3200):  # 100 ms frames, like the glasses / simulator
        ws.send_bytes(pcm[i : i + 3200])
    ws.send_json({"type": "voice", "action": "stop"})
    return recv(ws, "voice")


def test_push_to_talk_arms_then_tap_tap_sends(demo: tuple[TestClient, Any]) -> None:
    client, fake = demo
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        heard = push_to_talk(ws)
        assert heard == {"type": "voice", "state": "heard", "text": "Close half",
                         "command": "close 50%", "ok": True, "note": None}  # fmt: skip
        frame = recv(ws, "hud", mode="confirm")
        assert frame["voice"]["text"] == "Close half" and frame["ui"]["label"] == "Close 50%"
        assert fake.calls == []  # voice only arms
        time.sleep(0.35)
        ws.send_json({"type": "intent", "action": "tap"})
        assert recv(ws, "order")["status"] == "filled"
    assert fake.calls[0][0] == "close" and fake.calls[0][1].fraction == 0.5


def test_stop_to_breakeven_note_clears_once_the_confirm_is_armed(tmp_path: Path) -> None:
    """The heard line first says it waits for the stop lookup; once that lands and the confirm is
    armed, the stale "loading..." note must not stay on the confirm screen (Day 4 simulator)."""
    for client, _fake in make_client(tmp_path, "Stop to breakeven."):
        with client.websocket_connect(f"/ws?code={CODE}") as ws:
            recv(ws, "hud")
            heard = push_to_talk(ws)
            assert heard["command"] == "stop to breakeven"
            assert heard["note"] == "loading the current stop-loss"
            frame = recv(ws, "hud", mode="confirm")
            for _ in range(10):  # the note is refreshed right after the lookup armed it
                if frame["voice"] and frame["voice"]["note"] is None:
                    break
                frame = recv(ws, "hud", mode="confirm")
            assert frame["ui"]["label"] == "SL to breakeven"
            assert frame["voice"] == {"state": "heard", "text": "Stop to breakeven", "note": None}


def test_audio_outside_push_to_talk_is_ignored(demo: tuple[TestClient, Any]) -> None:
    client, fake = demo
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        ws.send_bytes(tone(0.5))  # no "start": dropped
        ws.send_json({"type": "voice", "action": "stop"})  # nothing to stop
        ws.send_json({"type": "intent", "action": "next"})
        assert recv(ws, "ack")["mode"] == "hud"
    assert fake.calls == []


def test_watch_mode_voice_is_blocked(tmp_path: Path) -> None:
    gen = make_client(
        tmp_path, "close half", watch_address="0x71491a31cfe1148ac412d5906545c5b65be67bd0"
    )
    client, _ = next(gen)
    from .test_state_feed import ch_msg

    apply_ws_message(
        client.app.state.market, ch_msg(user="0x71491a31cfe1148ac412d5906545c5b65be67bd0")
    )  # type: ignore[attr-defined]
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        heard = push_to_talk(ws)
        assert heard["command"] == "close 50%"
        result = recv(ws, "hud", mode="result")
        assert result["watch"] is True and result["ui"]["status"] == "blocked"
        assert result["voice"]["text"] == "close half"
    with pytest.raises(StopIteration):
        next(gen)


def test_voice_off_says_so(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), run_feed=False, run_bot=False, heartbeat_s=0.2)
    with TestClient(app) as client, client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.send_json({"type": "voice", "action": "start"})
        err = recv(ws, "voice")
        assert err["state"] == "error" and "VOICE_ENABLED=false" in err["note"]


def test_unknown_speech_shows_the_reason(tmp_path: Path) -> None:
    gen = make_client(tmp_path, "buy some bitcoin")
    client, fake = next(gen)
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        heard = push_to_talk(ws)
        assert (heard["command"], heard["ok"], heard["note"]) == (None, False, "not a command")
    assert fake.calls == []


def test_status_answers_on_the_glasses(tmp_path: Path) -> None:
    gen = make_client(tmp_path, "status")
    client, _ = next(gen)
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        heard = push_to_talk(ws)
        assert heard["command"] == "status"
        assert heard["note"] == "1 pos  uPnL +$41.20  DEMO"


def test_holding_too_long_transcribes_what_was_said(demo: tuple[TestClient, Any]) -> None:
    client, _ = demo
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        ws.send_json({"type": "voice", "action": "start"})
        recv(ws, "voice")
        ws.send_bytes(tone(MAX_SECONDS + 1))  # one huge chunk: cut at 8 s and transcribed
        assert recv(ws, "voice")["command"] == "close 50%"


def test_dataclass_position_fixture_is_consistent() -> None:
    assert dataclasses.replace(P, size=1.0).size == 1.0
