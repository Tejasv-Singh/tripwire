"""Forward-to-bot drafts (item 6): flow, Telegram handler replies, glasses end to end."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tripwire.actions import ActionFlow
from tripwire.drafts import OrderDraft, parse_draft
from tripwire.exchanges.fake import FakeAdapter
from tripwire.hl.feed import apply_ws_message
from tripwire.main import create_app
from tripwire.orders import OpenOrder
from tripwire.policy import Decision, KillSwitch, PolicyGuard, PolicyLimits
from tripwire.state import MarketState
from tripwire.telegram_bot import DRAFT_HEADER, BotServices, make_draft_handler

from .test_app import CODE, ETH_ROW, recv, settings

GUARD = PolicyGuard(PolicyLimits(), KillSwitch(False, None))
OK_DRAFT = parse_draft("BTC long 96.5k sl 95k tp 99k")
BIG_DRAFT = parse_draft("BTC long 96.5k sl 95k tp 99k $1000")


def decide(d: OrderDraft) -> Decision:
    return GUARD.check(d.to_order(), position=None, mark=96_400.0)


# --- flow -----------------------------------------------------------------------------------


def test_allowed_draft_offers_place_and_discard() -> None:
    flow = ActionFlow()
    assert flow.raise_draft(OK_DRAFT, decide(OK_DRAFT)) is True
    ui = flow.ui(0, trading=True)
    assert ui["kind"] == "draft" and ui["items"] == ["Place order", "Discard"]
    assert ui["draft"]["entry_px"] == 96_500 and ui["policy"]["allowed"] is True


def test_blocked_draft_only_offers_discard_and_says_why() -> None:
    flow = ActionFlow()
    flow.raise_draft(BIG_DRAFT, decide(BIG_DRAFT))
    ui = flow.ui(0, trading=True)
    assert ui["items"] == ["Discard"]
    assert ui["policy"] == {
        "allowed": False,
        "rule": "max_notional",
        "reason": "Notional $1,000 > max $250",
    }
    out = flow.handle("tap", position=None, now=0, trading=True)
    assert flow.mode == "hud" and out.note == "draft discarded"


def test_place_order_goes_through_precheck_and_two_taps() -> None:
    flow = ActionFlow()
    flow.raise_draft(OK_DRAFT, decide(OK_DRAFT))

    def precheck(order: Any) -> Decision:
        return GUARD.check(order, position=None, mark=96_400.0)

    flow.handle("tap", position=None, now=1, trading=True, precheck=precheck, ref_px=96_400.0)
    assert flow.mode == "confirm"
    ui = flow.ui(1, trading=True)
    assert (ui["action"], ui["notional"], ui["leverage"], ui["entry"], ui["sl"], ui["tp"]) == (
        "open", 100.0, 1.0, 96_500.0, 95_000.0, 99_000.0,
    )  # fmt: skip
    assert ui["size"] == pytest.approx(100 / 96_500)
    out = flow.handle("tap", position=None, now=1.5, trading=True)
    assert out.execute == OpenOrder("BTC", "LONG", 100.0, 1.0, 96_500.0, 95_000.0, 99_000.0)


def test_kill_switch_between_draft_and_place_blocks_at_arm() -> None:
    flow = ActionFlow()
    flow.raise_draft(OK_DRAFT, decide(OK_DRAFT))
    killed = PolicyGuard(PolicyLimits(), KillSwitch(True, None))
    flow.handle("tap", position=None, now=1, trading=True,
                precheck=lambda o: killed.check(o, position=None, mark=96_400.0))  # fmt: skip
    ui = flow.ui(1, trading=True)
    assert (
        ui["status"] == "blocked"
        and ui["message"] == "Blocked: Kill switch ON - only reduce-only orders"
    )


def test_draft_waits_for_a_confirm_and_needs_trading() -> None:
    p = None
    flow = ActionFlow()
    flow.raise_draft(OK_DRAFT, decide(OK_DRAFT))
    flow.handle("tap", position=p, now=1, trading=True)
    assert flow.mode == "confirm"
    assert flow.raise_draft(BIG_DRAFT, decide(BIG_DRAFT)) is False  # waits
    flow.handle("back", position=p, now=2, trading=True)
    assert flow.kind == "draft" and flow.ui(2, trading=True)["policy"]["allowed"] is False
    off = ActionFlow()
    off.raise_draft(OK_DRAFT, decide(OK_DRAFT))
    off.handle("tap", position=p, now=1, trading=False)
    assert off.ui(1, trading=False)["message"].startswith("Trading off")


# --- Telegram handler ------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text, self.caption = text, None
        self.chat_id, self.message_id, self.forward_origin = 7, 1, object()
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def update(text: str, user_id: int = 42) -> Any:
    return SimpleNamespace(message=FakeMessage(text), effective_user=SimpleNamespace(id=user_id))


def services(pushed: list[tuple[OrderDraft, Decision]]) -> BotServices:
    return BotServices(
        owner_id=42,
        known_coins=lambda: None,
        check_draft=decide,
        push_draft=lambda d, dec: pushed.append((d, dec)) or 1,
    )


async def test_owner_draft_is_checked_and_pushed() -> None:
    pushed: list[tuple[OrderDraft, Decision]] = []
    handler = make_draft_handler(services(pushed))
    u = update("BTC long 96.5k sl 95k tp 99k")
    await handler.callback(u, None)  # type: ignore[arg-type]
    (reply,) = u.message.replies
    assert reply.splitlines() == [
        DRAFT_HEADER,
        "BTC LONG $100 1x @ 96,500  SL 95,000  TP 99,000",
        "Defaults used: notional, leverage",
        "Policy: OK - within limits, has a stop-loss",
        "On your glasses now: two taps to confirm.",
    ]
    assert pushed[0][0] == OK_DRAFT


async def test_over_limit_draft_is_blocked_with_a_clear_reason() -> None:
    pushed: list[tuple[OrderDraft, Decision]] = []
    u = update("BTC long 96.5k sl 95k tp 99k $1000")
    await make_draft_handler(services(pushed)).callback(u, None)  # type: ignore[arg-type]
    assert "BLOCKED by policy: Notional $1,000 > max $250" in u.message.replies[0]
    assert pushed[0][1].allowed is False  # still shown on the glasses, with only "Discard"


async def test_ambiguous_text_and_strangers_get_a_reason() -> None:
    u = update("BTC long 96.5k 95k")
    await make_draft_handler(services([])).callback(u, None)  # type: ignore[arg-type]
    assert u.message.replies == [
        "Not an order I can draft: which price is 95k? label it (sl / tp / @entry)."
    ]
    stranger = update("BTC long 96.5k sl 95k", user_id=7)
    await make_draft_handler(services([])).callback(stranger, None)  # type: ignore[arg-type]
    assert stranger.message.replies == [
        "Order drafts from forwarded messages are only for the bot owner."
    ]


# --- end to end -----------------------------------------------------------------------------


@pytest.fixture
def draft_client(tmp_path: Path) -> Iterator[tuple[TestClient, FakeAdapter]]:
    holder: list[FakeAdapter] = []

    def make(state: MarketState) -> FakeAdapter:
        apply_ws_message(
            state, {"channel": "allMids", "data": {"mids": {"ETH": "3494.4", "BTC": "96400"}}}
        )
        holder.append(FakeAdapter(state, [ETH_ROW]))
        return holder[0]

    app = create_app(settings(tmp_path, telegram_owner_id=42), run_feed=False, run_bot=False,
                     heartbeat_s=0.2, make_adapter=make)  # fmt: skip
    with TestClient(app) as c:
        yield c, holder[0]


def forward(client: TestClient, text: str) -> list[str]:
    handler = make_draft_handler(client.app.state.bot_services)  # type: ignore[attr-defined]
    u = update(text)
    client.portal.call(handler.callback, u, None)  # type: ignore[union-attr]
    return list(u.message.replies)


def test_forwarded_draft_reaches_glasses_and_places(
    draft_client: tuple[TestClient, FakeAdapter],
) -> None:
    client, fake = draft_client
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        replies = forward(client, "BTC long 96.5k sl 95k tp 99k")
        assert "On your glasses now" in replies[0]
        draft = recv(ws, "hud", mode="menu")["ui"]
        assert draft["kind"] == "draft" and draft["items"] == ["Place order", "Discard"]
        ws.send_json({"type": "intent", "action": "tap"})  # Place order: arm
        assert recv(ws, "ack")["mode"] == "confirm"
        time.sleep(0.35)
        ws.send_json({"type": "intent", "action": "tap"})  # send
        order = recv(ws, "order")
        assert order["kind"] == "open" and order["status"] == "resting"
    assert fake.calls[0][0] == "place"


def test_over_limit_forward_is_blocked_on_the_glasses(
    draft_client: tuple[TestClient, FakeAdapter],
) -> None:
    client, fake = draft_client
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        recv(ws, "hud")
        forward(client, "BTC long 96.5k sl 95k tp 99k 10x")
        draft = recv(ws, "hud", mode="menu")["ui"]
        assert draft["policy"] == {
            "allowed": False,
            "rule": "max_leverage",
            "reason": "Leverage 10x > max 5x",
        }
        assert draft["items"] == ["Discard"]
    assert fake.calls == []


def test_draft_forwarded_with_no_glasses_waits_for_the_next_connect(
    draft_client: tuple[TestClient, FakeAdapter],
) -> None:
    client, _ = draft_client
    replies = forward(client, "BTC long 96.5k sl 95k tp 99k")
    assert "No glasses connected" in replies[0]
    with client.websocket_connect(f"/ws?code={CODE}") as ws:
        assert recv(ws, "hud", mode="menu")["ui"]["kind"] == "draft"
