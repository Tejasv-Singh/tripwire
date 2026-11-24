"""Session summary (Day 3, item 3): tracker maths, journal text, screens, /summary, journal post,
WATCH realized P&L via public fills."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tripwire.actions import ActionFlow
from tripwire.exchanges.fake import FakeAdapter
from tripwire.exchanges.hyperliquid import HyperliquidAdapter
from tripwire.hl.feed import apply_ws_message
from tripwire.main import create_app
from tripwire.orders import CloseOrder, OrderResult, StopOrder
from tripwire.session import SessionTracker, usd
from tripwire.state import MarketState
from tripwire.telegram_bot import BotServices, make_member_handler, make_summary_handler

from .test_app import CODE, ETH_ROW, recv, settings
from .test_trading import TESTNET_API_URL, FakeInfo


class Clock:
    def __init__(self) -> None:
        self.ms = 1_795_600_000_000

    def __call__(self) -> int:
        return self.ms


CLOSE = CloseOrder("ETH", "LONG", 0.5, 0.25)
FILLED = OrderResult(True, "filled", "Filled 0.25 @ 1,800", 0.25, 1800.0)


# --- tracker -----------------------------------------------------------------------------------


def test_counts_trades_dry_runs_blocks_alerts() -> None:
    t = SessionTracker(clock_ms=Clock())
    t.record_order(CLOSE, FILLED)
    t.record_order(CLOSE, FILLED, via_alert=True)
    t.record_order(CLOSE, OrderResult(True, "dry_run", "DRY RUN: ..."))
    t.record_order(CLOSE, OrderResult(False, "blocked", "Kill switch ON"))
    t.record_order(CLOSE, OrderResult(False, "rejected", "no liquidity"))  # not a trade
    t.record_block()
    t.record_alert(), t.record_alert(), t.record_snooze()
    s = t.snapshot("own account")
    assert (s.trades, s.dry_runs, s.policy_blocks) == (2, 1, 2)
    assert (s.alerts_fired, s.alerts_acted, s.alerts_snoozed) == (2, 1, 1)
    assert s.trade_log[1].via_alert and s.trade_log[0].fraction == 0.5


def test_session_pnl_and_max_drawdown_ignore_the_starting_unrealized() -> None:
    t = SessionTracker(clock_ms=Clock())
    t.unrealized_start = t.unrealized = 500.0  # a WATCH position already +$500 at start
    for upnl in (520.0, 480.0, 450.0, 510.0):  # +20, -20, -50, +10 vs the start
        t.sample(upnl)
    assert t.session_pnl == pytest.approx(10.0)
    assert t.max_drawdown == pytest.approx(-70.0)  # peak +20 -> trough -50
    t.sample(510.0, realized=-15.0)  # a losing fill
    assert t.session_pnl == pytest.approx(-5.0)


def test_end_starts_a_fresh_session_from_the_current_unrealized() -> None:
    clock = Clock()
    t = SessionTracker(clock_ms=clock)
    t.record_order(CLOSE, FILLED)
    t.sample(30.0)
    clock.ms += 90 * 60 * 1000
    ended = t.end("own account")
    assert ended.ended_ms == clock.ms and ended.duration_s == 5400 and ended.trades == 1
    assert t.started_ms == clock.ms and t.trade_log == [] and t.unrealized_start == 30.0
    assert t.session_pnl == 0.0


def test_journal_text() -> None:
    clock = Clock()
    t = SessionTracker(clock_ms=clock)
    t.record_order(CLOSE, FILLED, via_alert=True)
    t.record_order(StopOrder("ETH", "LONG", 1700, 1600, 1, "SL to breakeven"),
                   OrderResult(True, "placed", "SL set at 1700"))  # fmt: skip
    t.sample(12.4, realized=8.0)
    t.record_alert(), t.record_snooze(), t.record_block()
    clock.ms += 75 * 60 * 1000
    text = t.end("WATCH 0x7149...7bd0 (read-only)").text()
    lines = text.splitlines()
    assert lines[0].startswith("Tripwire session ") and lines[0].endswith(
        "(1h15m) - Hyperliquid TESTNET"
    )
    assert lines[1] == "Mode: WATCH 0x7149...7bd0 (read-only)"
    assert lines[2] == "Trades: 2"
    assert lines[3].endswith("close 50% ETH LONG: Filled 0.25 @ 1,800 (from alert)")
    assert lines[4].endswith("stop ETH LONG: SL set at 1700")
    assert lines[5] == "Realized: +$8.00 | Unrealized now: +$12.40 | Session P&L: +$20.40"
    assert lines[7] == "Alerts: 1 fired, 1 acted, 1 snoozed | Policy blocks: 1"
    assert lines[-1] == "Numbers only - not advice."
    assert usd(-3.1) == "-$3.10"


# --- flow screens ------------------------------------------------------------------------------


def test_double_tap_on_hud_asks_then_tap_ends_then_tap_posts() -> None:
    flow = ActionFlow()
    out = flow.handle("back", position=None, now=0, trading=True)
    assert flow.mode == "end" and "end session?" in (out.note or "")
    assert flow.handle("next", position=None, now=0, trading=True).note == "session continues"
    assert flow.mode == "hud"
    flow.handle("back", position=None, now=0, trading=True)
    assert flow.handle("tap", position=None, now=0, trading=True).end_session
    flow.show_summary({"trades": 2}, "TEXT")
    assert flow.ui(0, trading=True) == {
        "mode": "summary", "coin": None, "summary": {"trades": 2}, "posted": None, "note": None,
    }  # fmt: skip
    assert flow.handle("tap", position=None, now=0, trading=True).post_summary
    assert flow.set_post_result(True, "posted to the journal")
    assert flow.handle("tap", position=None, now=0, trading=True).note == "already posted"
    flow.handle("prev", position=None, now=0, trading=True)
    assert flow.mode == "hud" and flow.summary is None


# --- Telegram ----------------------------------------------------------------------------------


async def test_summary_command_is_owner_only() -> None:
    handler = make_summary_handler(BotServices(owner_id=42, summary_text=lambda: "SUMMARY"))
    for uid, expected in ((42, "SUMMARY"), (7, "Not authorized.")):
        got: list[str] = []

        async def reply(text: str, sink: list[str] = got) -> None:
            sink.append(text)

        u = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=uid),
        )
        await handler.callback(u, None)  # type: ignore[arg-type]
        assert got == [expected]


async def test_added_to_a_channel_dms_the_owner_its_id() -> None:
    sent: list[dict[str, Any]] = []

    async def send_message(**kw: Any) -> None:
        sent.append(kw)

    handler = make_member_handler(BotServices(owner_id=42, journal_chat_id=None))
    chat = SimpleNamespace(id=-1001234567890, type="channel", title="Tripwire journal")
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            chat=chat, new_chat_member=SimpleNamespace(status="administrator")
        )
    )
    await handler.callback(update, SimpleNamespace(bot=SimpleNamespace(send_message=send_message)))  # type: ignore[arg-type]
    (msg,) = sent
    assert msg["chat_id"] == 42 and "JOURNAL_CHAT_ID=-1001234567890" in msg["text"]
    sent.clear()
    update.my_chat_member.new_chat_member.status = "member"
    await handler.callback(update, SimpleNamespace(bot=SimpleNamespace(send_message=send_message)))  # type: ignore[arg-type]
    assert "Make me an ADMIN" in sent[0]["text"]


# --- WATCH: realized P&L of the watched account (public fills) ---------------------------------


async def test_watch_session_realized_uses_the_watched_address() -> None:
    watched = "0x71491a31cfe1148ac412d5906545c5b65be67bd0"
    info = FakeInfo()
    info.fills = [{"closedPnl": "12.5", "fee": "0.5"}]
    seen: list[str] = []
    orig = info.user_fills_by_time

    def spy(address: str, start: int) -> Any:
        seen.append(address)
        return orig(address, start)

    info.user_fills_by_time = spy  # type: ignore[method-assign]
    a = HyperliquidAdapter(MarketState(account=watched, watch=True), base_url=TESTNET_API_URL,
                           account_address="", watch=True, info_factory=lambda: info)  # fmt: skip
    assert await a.realized_pnl_since(0) == 12.0 and seen == [watched]
    assert await a.realized_pnl_today() == 0.0  # the loss lock never looks at someone else


# --- end to end ----------------------------------------------------------------------------------


@pytest.fixture
def app_client(tmp_path: Path) -> Iterator[tuple[TestClient, FakeAdapter]]:
    holder: list[FakeAdapter] = []

    def make(state: MarketState) -> FakeAdapter:
        apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
        holder.append(FakeAdapter(state, [ETH_ROW]))
        return holder[0]

    app = create_app(settings(tmp_path, journal_chat_id=-1001), run_feed=False, run_bot=False,
                     heartbeat_s=0.2, make_adapter=make)  # fmt: skip
    with TestClient(app) as c:
        yield c, holder[0]


def test_trade_then_end_session_then_post(
    app_client: tuple[TestClient, FakeAdapter], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app_client
    posted: list[tuple[int, str]] = []

    async def fake_send(self: Any, chat_id: int, text: str) -> tuple[bool, str]:
        posted.append((chat_id, text))
        return True, "posted to the journal"

    monkeypatch.setattr("tripwire.telegram_bot.TelegramBot.send_to_chat", fake_send)
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        for action in ("tap", "next", "tap"):  # Close 50%: arm
            ws.send_json({"type": "intent", "action": action})
            recv(ws, "ack")
        time.sleep(0.35)
        ws.send_json({"type": "intent", "action": "tap"})
        assert recv(ws, "order")["status"] == "filled"
        ws.send_json({"type": "intent", "action": "back"})  # result -> hud
        recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "back"})  # double tap on the HUD
        assert recv(ws, "ack")["mode"] == "end"
        live = recv(ws, "hud", mode="end")["ui"]["summary"]
        assert live["trades"] == 1 and live["ended_ms"] is None
        ws.send_json({"type": "intent", "action": "tap"})  # end it
        summary = recv(ws, "hud", mode="summary")["ui"]
        assert summary["summary"]["trades"] == 1 and summary["summary"][
            "realized"
        ] == pytest.approx(20.6, abs=0.01)
        ws.send_json({"type": "intent", "action": "tap"})  # post to the journal
        for _ in range(20):
            ui = recv(ws, "hud", mode="summary")["ui"]
            if ui["posted"] is not None:
                break
        assert ui["posted"] is True and ui["note"] == "posted to the journal"
    (chat_id, text) = posted[0]
    assert chat_id == -1001 and "Trades: 1" in text and "close 50% ETH LONG" in text
    rows = client.app.state.store.recent_sessions()  # type: ignore[attr-defined]
    assert len(rows) == 1
    assert client.get("/healthz").json()["session"]["trades"] == 0  # a fresh session runs now


def test_post_without_journal_says_why(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), run_feed=False, run_bot=False, heartbeat_s=0.2)
    with TestClient(app) as client, client.websocket_connect(f"/ws?code={CODE}") as ws:
        ws.send_json({"type": "intent", "action": "back"})
        recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "tap"})
        recv(ws, "hud", mode="summary")
        ws.send_json({"type": "intent", "action": "tap"})
        for _ in range(20):
            ui = recv(ws, "hud", mode="summary")["ui"]
            if ui["posted"] is not None:
                break
        assert ui["posted"] is False and ui["note"] == "JOURNAL_CHAT_ID not set in .env"
