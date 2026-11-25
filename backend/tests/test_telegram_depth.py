"""Telegram depth part 2 (item 6): inbox HUD line, reply drafts after a fill, /status."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tripwire.actions import ActionFlow, ReplyOffer
from tripwire.exchanges.fake import FakeAdapter
from tripwire.hl.feed import apply_ws_message
from tripwire.inbox import PREVIEW_CHARS, Inbox, ReplyTarget
from tripwire.main import create_app
from tripwire.orders import OrderResult
from tripwire.replies import (
    MAX_REPLY_CHARS,
    ReplyContext,
    ReplyDrafter,
    clean_reply,
    template_reply,
)
from tripwire.state import MarketState
from tripwire.telegram_bot import BotServices, make_business_message_handler, make_status_handler

from .test_app import CODE, ETH_ROW, recv, settings

TARGET = ReplyTarget("bc-1", 555, "@alice", 0.0)


# --- inbox ----------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_inbox_keeps_30_chars_counts_and_expires() -> None:
    clock, woke = Clock(), []
    inbox = Inbox(window_s=600, on_change=lambda: woke.append(1), clock=clock)
    assert inbox.summary() is None
    long_text = "hey   are you still in that ETH long or did you close it already?"
    inbox.add(connection_id="bc-1", chat_id=555, name="@alice", text=long_text)
    s = inbox.summary()
    assert s == {"count": 1, "from": "@alice", "preview": "hey are you still in that ETH "}
    assert len(s["preview"]) == PREVIEW_CHARS and woke == [1]
    assert all(long_text not in repr(v) for v in vars(inbox).values())  # full text not kept
    clock.t = 300
    inbox.add(connection_id="bc-1", chat_id=556, name="Bob", text="gm")
    assert inbox.summary() == {"count": 2, "from": "Bob", "preview": "gm"}
    assert inbox.reply_target() == ReplyTarget("bc-1", 556, "Bob", 300)
    clock.t = 700  # first message is 700 s old: out of the 10-minute window
    assert inbox.summary()["count"] == 1  # type: ignore[index]
    clock.t = 300 + 1801
    assert inbox.reply_target() is None and inbox.summary() is None


def msg(text: str, user_id: int, username: str | None = "alice") -> Any:
    return SimpleNamespace(
        text=text, caption=None, business_connection_id="bc-1", chat_id=555, message_id=9,
        from_user=SimpleNamespace(id=user_id, username=username, first_name="Alice"),
    )  # fmt: skip


async def test_business_messages_fill_the_inbox_but_not_own_or_edited_ones() -> None:
    inbox = Inbox()
    handler = make_business_message_handler(BotServices(owner_id=42, inbox=inbox))
    own = SimpleNamespace(business_message=msg("my own text", 42), edited_business_message=None)
    await handler.callback(own, None)  # type: ignore[arg-type]
    assert inbox.summary() is None
    edited = SimpleNamespace(business_message=None, edited_business_message=msg("x", 7))
    await handler.callback(edited, None)  # type: ignore[arg-type]
    assert inbox.summary() is None
    theirs = SimpleNamespace(business_message=msg("are you out?", 7), edited_business_message=None)
    await handler.callback(theirs, None)  # type: ignore[arg-type]
    assert inbox.summary() == {"count": 1, "from": "@alice", "preview": "are you out?"}
    noname = SimpleNamespace(business_message=msg("yo", 8, None), edited_business_message=None)
    await handler.callback(noname, None)  # type: ignore[arg-type]
    assert inbox.summary()["from"] == "Alice"  # type: ignore[index]


# --- reply drafts ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ctx", "text"),
    [
        (ReplyContext("close", "ETH", "LONG", 0.5), "Trimmed half of my ETH."),
        (ReplyContext("close", "ETH", "LONG", 0.25), "Trimmed a quarter of my ETH."),
        (ReplyContext("close", "ETH", "LONG", 1.0), "Closed my ETH long."),
        (
            ReplyContext("close", "ETH", "LONG", 0.5, sl=1700.0),
            "Trimmed half of my ETH, SL moved to 1,700.",
        ),
        (ReplyContext("stop", "ETH", "LONG", sl=1700.0), "Moved my ETH stop to 1,700."),
        (ReplyContext("open", "BTC", "LONG"), "Opened a small BTC long."),
    ],
)
def test_templates_are_short_and_factual(ctx: ReplyContext, text: str) -> None:
    assert template_reply(ctx) == text and len(text) <= MAX_REPLY_CHARS


def test_clean_reply() -> None:
    assert clean_reply('  "Trimmed half, all good."  \nsecond line') == "Trimmed half, all good."
    assert clean_reply("x" * 61) is None and clean_reply("") is None and clean_reply(None) is None


def transport(
    status: int, content: str | None = None, seen: list[Any] | None = None
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = {"choices": [{"message": {"content": content}}]} if content is not None else {}
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


async def test_openrouter_draft_sends_only_trade_facts() -> None:
    seen: list[httpx.Request] = []
    d = ReplyDrafter(
        "sk-or-test", "some/model:free", transport=transport(200, "Took half off ETH.", seen)
    )
    ctx = ReplyContext("close", "ETH", "LONG", 0.5, price=1684.9)
    assert await d.draft(ctx) == ("Took half off ETH.", "openrouter")
    (req,) = seen
    body = json.loads(req.content)
    assert req.url == "https://openrouter.ai/api/v1/chat/completions"
    assert (
        req.headers["Authorization"] == "Bearer sk-or-test" and body["model"] == "some/model:free"
    )
    assert body["messages"][1]["content"] == (
        "Facts: I just closed half of my ETH long at about 1,684.9. Write the reply."
    )


@pytest.mark.parametrize(
    "t",
    [transport(429), transport(500), transport(200, "x" * 80), transport(200, "   ")],
    ids=["rate-limited", "server-error", "too-long", "empty"],
)
async def test_openrouter_problems_fall_back_to_the_template(t: httpx.MockTransport) -> None:
    d = ReplyDrafter("sk-or-test", transport=t)
    assert await d.draft(ReplyContext("close", "ETH", "LONG", 0.5)) == (
        "Trimmed half of my ETH.",
        "template",
    )


async def test_no_key_means_templates_and_no_request() -> None:
    seen: list[Any] = []
    d = ReplyDrafter("", transport=transport(200, "hi", seen))
    assert (await d.draft(ReplyContext("stop", "ETH", "LONG", sl=1700.0)))[
        1
    ] == "template" and seen == []


# --- flow: reply screen ---------------------------------------------------------------------


def result_screen(flow: ActionFlow) -> int:
    out = flow.finish(OrderResult(True, "filled", "Filled 0.25 @ 1,684.9"))
    return out.token


def test_reply_offer_replaces_the_result_and_tap_sends() -> None:
    flow = ActionFlow()
    token = result_screen(flow)
    offer = ReplyOffer(TARGET, "Trimmed half of my ETH.", "template")
    assert flow.offer_reply(offer, token) is not None and flow.mode == "reply"
    assert flow.ui(0, trading=True) == {
        "mode": "reply", "coin": None, "to": "@alice", "text": "Trimmed half of my ETH.",
        "source": "template", "result_status": "filled", "result_message": "Filled 0.25 @ 1,684.9",
    }  # fmt: skip  # the fill stays on screen above the offer (found in the simulator check)
    assert flow.handle("next", position=None, now=0, trading=True).send_reply is None  # swipe: no
    out = flow.handle("tap", position=None, now=0, trading=True)
    assert out.send_reply == offer and flow.mode == "hud"


def test_double_tap_skips_and_stale_offers_are_dropped() -> None:
    flow = ActionFlow()
    token = result_screen(flow)
    flow.offer_reply(ReplyOffer(TARGET, "x", "template"), token)
    out = flow.handle("back", position=None, now=0, trading=True)
    assert flow.mode == "hud" and out.note == "reply skipped" and out.send_reply is None
    late = ActionFlow()
    token = result_screen(late)
    late.handle("tap", position=None, now=0, trading=True)  # user already left the result
    assert (
        late.offer_reply(ReplyOffer(TARGET, "x", "template"), token) is None and late.mode == "hud"
    )


# --- /status --------------------------------------------------------------------------------


async def test_status_is_owner_only() -> None:
    handler = make_status_handler(BotServices(owner_id=42, status_text=lambda: "STATUS"))
    for uid, expected in ((42, "STATUS"), (7, "Not authorized.")):
        replies: list[str] = []

        async def reply(text: str, sink: list[str] = replies) -> None:
            sink.append(text)

        u = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=uid),
        )
        await handler.callback(u, None)  # type: ignore[arg-type]
        assert replies == [expected]


# --- end to end -----------------------------------------------------------------------------


@pytest.fixture
def tg_client(tmp_path: Path) -> Iterator[tuple[TestClient, FakeAdapter]]:
    holder: list[FakeAdapter] = []

    def make(state: MarketState) -> FakeAdapter:
        apply_ws_message(state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4"}}})
        holder.append(FakeAdapter(state, [ETH_ROW]))
        return holder[0]

    app = create_app(settings(tmp_path, telegram_owner_id=42), run_feed=False, run_bot=False,
                     heartbeat_s=0.2, make_adapter=make)  # fmt: skip
    with TestClient(app) as c:
        yield c, holder[0]


def test_status_text_lists_positions_and_upnl(tg_client: tuple[TestClient, FakeAdapter]) -> None:
    client, _ = tg_client
    text = client.app.state.bot_services.status_text()  # type: ignore[attr-defined]
    lines = text.splitlines()
    assert lines[0] == "Tripwire status (Hyperliquid TESTNET)"
    assert (
        lines[1] == "Positions: price only (no account)  |  Trading: DEMO (in-memory fake exchange)"
    )
    assert lines[2] == "Kill switch: off  |  Alerts at 5% liq distance"
    assert lines[3] == "Away mode: ON - no glasses connected, risk alerts come here"
    assert lines[4].startswith("ETH-PERP LONG 0.5 @ 3,412  mark 3,494.4  uPnL +41.20 (+2.4%)")
    assert lines[-2] == "Total uPnL: +41.20 USD"
    assert lines[-1].startswith("Latency (HL event -> glasses render):")


def test_inbox_line_then_reply_offer_after_a_fill(
    tg_client: tuple[TestClient, FakeAdapter], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, fake = tg_client
    sent: list[tuple[ReplyTarget, str]] = []

    async def fake_send(self: Any, connection_id: str, chat_id: int, text: str) -> bool:
        sent.append((ReplyTarget(connection_id, chat_id, "", 0), text))
        return True

    monkeypatch.setattr("tripwire.telegram_bot.TelegramBot.send_business_reply", fake_send)
    inbox: Inbox = client.app.state.inbox  # type: ignore[attr-defined]
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        client.portal.call(lambda: inbox.add(connection_id="bc-1", chat_id=555, name="@alice",  # type: ignore[union-attr]
                                             text="still in ETH?"))  # fmt: skip
        for _ in range(20):
            hud = recv(ws, "hud")
            if hud["inbox"]:
                break
        assert hud["inbox"] == {"count": 1, "from": "@alice", "preview": "still in ETH?"}
        ws.send_json({"type": "intent", "action": "tap"})
        recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "next"})  # Close 50%
        recv(ws, "ack")
        ws.send_json({"type": "intent", "action": "tap"})
        recv(ws, "ack")
        time.sleep(0.35)
        ws.send_json({"type": "intent", "action": "tap"})
        assert recv(ws, "order")["status"] == "filled"
        offer = recv(ws, "hud", mode="reply")["ui"]
        assert offer["to"] == "@alice" and offer["source"] == "template"
        assert offer["text"] == "Trimmed half of my ETH."
        ws.send_json({"type": "intent", "action": "tap"})  # send it
        assert recv(ws, "reply") == {"type": "reply", "ok": True, "to": "@alice"}
    assert sent == [(ReplyTarget("bc-1", 555, "", 0), "Trimmed half of my ETH.")]
