"""`/ws?code=<PAIRING_CODE>` — the glasses' only connection to the backend.

Per connection:
  * a sender task pushes a "hud" frame whenever the market state (or this client's screen)
    changes — at most one frame every `min_interval_s` (500 ms) — plus a heartbeat frame every
    `heartbeat_s` so the glasses can show feed age / detect a dead link;
  * a receiver loop accepts intents {"type":"intent","action":"next"|"prev"|"tap"|"back"} and
    feeds them to this client's ActionFlow (actions.py: menu -> two-tap confirm -> order);
  * a confirmed close runs as a background task (OrderExecutor, trading.py). It is deliberately
    NOT cancelled if the glasses disconnect: the order may already be on its way, and its result
    must still be logged;
  * push-to-talk: {"type":"voice","action":"start"}, then BINARY frames of raw PCM (16 kHz s16le
    mono) while the long-press is held, then {"type":"voice","action":"stop"} -> local
    faster-whisper (voice.py) -> voice_grammar.py -> ActionFlow.voice(), which only ARMS.
    The transcript shows on the glasses for VOICE_OVERLAY_S.
Protocol reference: docs/ws-protocol.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from tripwire.actions import REPLY_HOLD_S, ActionFlow, ReplyOffer
from tripwire.db import Store
from tripwire.drafts import OrderDraft
from tripwire.exchanges.base import ExchangeAdapter
from tripwire.hl.positions import Position
from tripwire.hud import (
    PROTOCOL_VERSION,
    apply_nav,
    build_hud_frame,
    next_send_delay,
    resolve_index,
)
from tripwire.inbox import Inbox, ReplyTarget
from tripwire.orders import Order, OrderResult, OrderService
from tripwire.policy import Decision, PolicyGuard
from tripwire.replies import ReplyContext, ReplyDrafter
from tripwire.risk import Alert
from tripwire.state import MarketState
from tripwire.voice import BYTES_PER_SECOND, MAX_SECONDS, VoiceEngine
from tripwire.voice_grammar import parse_voice

log = logging.getLogger(__name__)

CLOSE_BAD_PAIRING_CODE = 4401
PENDING_DRAFT_S = 300.0  # a draft forwarded while no glasses were connected waits this long
MIN_INTERVAL_S = 0.5
HEARTBEAT_S = 1.0  # a frame at least every second, so the glasses can flag >3 s as STALE
VOICE_OVERLAY_S = 2.0  # how long the transcript stays on the glasses
STATUS_OVERLAY_S = 4.0  # voice "status" answer
MAX_VOICE_BYTES = int(MAX_SECONDS * BYTES_PER_SECOND)


class IntentMessage(BaseModel):
    type: Literal["intent"]
    action: Literal["next", "prev", "tap", "back"]


class VoiceMessage(BaseModel):
    type: Literal["voice"]
    action: Literal["start", "stop", "cancel"]


class RenderedMessage(BaseModel):
    """The plugin finished drawing the frame `seq`, which was built from data received at src_ms."""

    type: Literal["rendered"]
    seq: int
    src_ms: int


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning("%s stopped: %s: %s", task.get_name(), type(exc).__name__, exc)


class ClientSession:
    """State for one connected glasses client."""

    def __init__(self, client: str) -> None:
        self.client = client
        self.index = 0
        self.seq = 0
        self.flow = ActionFlow()
        self.timers: list[asyncio.TimerHandle] = []
        self.connected = True
        self.dirty = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.voice_buf: bytearray | None = None  # not None while push-to-talk is held
        self.voice_overlay: dict[str, Any] | None = None  # {"state", "text", "note"} on screen
        self.voice_token = 0

    def wake(self) -> None:
        self.dirty.set()

    def cancel_timers(self) -> None:
        for handle in self.timers:
            handle.cancel()
        self.timers.clear()


class ClientHub:
    """Registry of connected glasses clients (the risk engine asks "is anyone looking?")."""

    def __init__(self) -> None:
        self.sessions: set[ClientSession] = set()
        self.pending_draft: tuple[OrderDraft, Decision, float] | None = None

    @property
    def count(self) -> int:
        return len(self.sessions)

    def raise_alert(self, alert: Alert) -> int:
        """Put the RISK screen on every connected client. Returns how many got it."""
        for session in list(self.sessions):
            session.flow.raise_alert(alert)
            session.wake()
        return len(self.sessions)

    def push_draft(self, draft: OrderDraft, decision: Decision, now: float) -> int:
        """Show a forwarded order draft on every client; if none is connected, keep it for the
        next one that connects within PENDING_DRAFT_S."""
        for session in list(self.sessions):
            session.flow.raise_draft(draft, decision)
            session.wake()
        self.pending_draft = None if self.sessions else (draft, decision, now)
        return len(self.sessions)

    def wake_all(self) -> None:
        for session in list(self.sessions):
            session.wake()

    def reset_clients(self) -> int:
        """npm run demo:reset: every connected client back to the plain HUD, nothing pending.
        Returns how many clients were reset."""
        self.pending_draft = None
        for session in list(self.sessions):
            session.cancel_timers()
            session.flow.reset()
            session.voice_overlay = None
            session.voice_buf = None
            session.wake()
        return len(self.sessions)

    def take_pending_draft(self, now: float) -> tuple[OrderDraft, Decision] | None:
        pending, self.pending_draft = self.pending_draft, None
        if pending is None or now - pending[2] > PENDING_DRAFT_S:
            return None
        return pending[0], pending[1]


def make_ws_router(
    *,
    state: MarketState,
    pairing_code: str,
    store: Store | None,
    adapter: ExchangeAdapter,
    hub: ClientHub,
    guard: PolicyGuard,
    orders: OrderService,
    on_snooze: Callable[[str, str, float], None] | None = None,
    inbox: Inbox | None = None,
    replier: ReplyDrafter | None = None,
    send_reply: Callable[[ReplyTarget, str], Awaitable[bool]] | None = None,
    voice: VoiceEngine | None = None,
    status_line: Callable[[], str] | None = None,
    audio_override: Callable[[], bytes | None] | None = None,  # demo only: substitute speech
    sessions: Any = None,  # session.SessionService (screen 6)
    latency: Any = None,  # latency.LatencyStats
    glyphs: str = "unicode",
    post_journal: Callable[[str], Awaitable[tuple[bool, str]]] | None = None,
    # Away mode (risk.py): an unresolved alert opens first on connect; order results close it
    active_alert: Callable[[], Alert | None] | None = None,
    on_order_result: Callable[[Order, OrderResult], None] | None = None,
    min_interval_s: float = MIN_INTERVAL_S,
    heartbeat_s: float = HEARTBEAT_S,
) -> APIRouter:
    router = APIRouter()
    expected = pairing_code.encode()
    order_tasks: set[asyncio.Task[None]] = set()  # strong refs so running orders aren't GC'd

    def trading_on() -> bool:
        return adapter.can_trade

    def trading_info() -> dict[str, Any]:
        return {
            "enabled": trading_on(),
            "status": adapter.status,
            "dry_run": adapter.dry_run,
            "exchange": adapter.name,
        }

    def selected_position(session: ClientSession) -> Position | None:
        if not state.positions:
            return None
        p = state.positions[resolve_index(state, session.index, session.flow.coin)]
        if session.flow.coin is not None and p.coin != session.flow.coin:
            return None  # the coin the menu is about is no longer open
        return p

    def live_position(coin: str) -> Position | None:
        return next((p for p in state.positions if p.coin == coin), None)

    def ref_price(coin: str) -> float | None:
        p = live_position(coin)
        return p.mark if p else (state.marks.get(coin) or state.mids.get(coin))

    def precheck(order: Order) -> Decision:
        return guard.check(order, position=live_position(order.coin), mark=ref_price(order.coin))

    async def send_order(order: Order) -> OrderResult:
        """The adapter call for each order kind -- only ever invoked by OrderService.submit()."""
        if order.kind == "close":
            return await adapter.close_reduce_only(order)
        if order.kind == "stop":
            return await adapter.set_sl(order)
        return await adapter.place_order(order)

    def schedule(session: ClientSession, delay_s: float, token: int) -> None:
        loop = asyncio.get_running_loop()

        def fire() -> None:
            if session.flow.on_timer(token):
                session.wake()

        session.timers = [h for h in session.timers if h.when() > loop.time()]
        session.timers.append(loop.call_later(delay_s, fire))

    async def send(ws: WebSocket, session: ClientSession, payload: dict[str, Any]) -> None:
        async with session.send_lock:  # sender task, receiver loop and order task all send
            await ws.send_json(payload)

    async def load_stop(session: ClientSession, coin: str) -> None:
        """The SL submenu needs the position's current stop-loss (SL to breakeven / tighter)."""
        try:
            info = await adapter.current_stop(coin)
        except Exception as exc:  # leave it "not loaded": never pretend there is no stop
            log.warning("current stop lookup for %s failed: %s", coin, exc)
            session.flow.note = f"SL lookup failed: {type(exc).__name__}"[:46]
            session.wake()
            return
        if session.flow.set_stop(coin, info):
            session.wake()
        pending = session.flow.take_pending_voice()  # a spoken "stop to breakeven" waited for this
        if pending is not None:
            loop = asyncio.get_running_loop()
            position = selected_position(session)
            out = session.flow.select_key(pending, **flow_ctx(session, position, loop.time()))
            apply_outcome(None, session, out, loop.time())
            overlay = session.voice_overlay
            if overlay is not None and overlay.get("state") == "heard":
                # "loading the current stop-loss" is over: show what the wait led to instead
                # (nothing once the confirm is armed, or the reason it wasn't)
                session.voice_overlay = {**overlay, "note": out.note}
            session.wake()

    def spawn(session: ClientSession, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=f"{name}-{session.client}")
        order_tasks.add(task)
        task.add_done_callback(order_tasks.discard)
        task.add_done_callback(_log_task_failure)

    async def offer_reply(
        session: ClientSession, req: Order, result: OrderResult, result_token: int
    ) -> None:
        """After a real fill: draft a <= 60-char reply to the last business chat and offer it."""
        target = inbox.reply_target() if inbox is not None else None
        if target is None or replier is None or send_reply is None:
            return
        ctx = ReplyContext(
            kind=req.kind,
            coin=req.coin,
            side=req.side,
            fraction=req.fraction if req.kind == "close" else None,
            price=result.avg_px,
            sl=req.trigger_px if req.kind == "stop" else None,
        )
        text, source = await replier.draft(ctx)
        token = session.flow.offer_reply(ReplyOffer(target, text, source), result_token)
        if token is not None and session.connected:
            schedule(session, REPLY_HOLD_S, token)
            session.wake()

    async def deliver_reply(ws: WebSocket, session: ClientSession, offer: ReplyOffer) -> None:
        ok = False
        if send_reply is not None:
            try:
                ok = await send_reply(offer.target, offer.text)
            except Exception:
                log.exception("reply send failed")
        log.info("business reply to %s: %s", offer.target.name, "sent" if ok else "FAILED")
        if session.connected:
            with contextlib.suppress(Exception):
                await send(ws, session, {"type": "reply", "ok": ok, "to": offer.target.name})

    async def end_session(session: ClientSession) -> None:
        summary = await sessions.end()
        session.flow.show_summary(summary.to_ui(), summary.text())
        session.wake()

    async def post_summary(session: ClientSession) -> None:
        if post_journal is None:
            ok, note = False, "journal not configured"
        else:
            try:
                ok, note = await post_journal(session.flow.summary_text)
            except Exception as exc:
                ok, note = False, f"{type(exc).__name__}: {exc}"[:60]
        if session.flow.set_post_result(ok, note):
            session.wake()

    async def run_order(
        ws: WebSocket, session: ClientSession, req: Order, via_alert: bool = False
    ) -> None:
        try:
            result = await orders.submit(  # policy guard, then the exchange
                req, position=live_position(req.coin), mark=ref_price(req.coin), send=send_order
            )
        except Exception as exc:  # an executor bug must not leave the screen stuck on "sending"
            log.exception("order task failed")
            result = OrderResult(False, "error", f"{type(exc).__name__}: {exc}"[:120])
        if on_order_result is not None:
            try:
                on_order_result(req, result)  # e.g. an alert acted on -> its episode closes
            except Exception:
                log.exception("on_order_result failed")
        out = session.flow.finish(result)
        if out.timer_s is not None and session.connected:
            schedule(session, out.timer_s, out.token)
        if result.ok and result.status in ("filled", "placed"):  # a real change, not a dry run
            spawn(session, offer_reply(session, req, result, out.token), "reply-draft")
        if sessions is not None:
            sessions.tracker.record_order(req, result, via_alert=via_alert)
            sessions.tracker.sample(sessions.unrealized_now())
        fraction = req.fraction if req.kind == "close" else None
        size = req.size if req.kind == "close" else None
        log.info("order %s %s from %s -> %s", req.kind, req.coin, session.client, result.status)
        if store is not None:
            try:
                await asyncio.to_thread(
                    store.log_order,
                    client=session.client,
                    coin=req.coin,
                    side=req.side,
                    kind=req.kind,
                    fraction=fraction if fraction is not None else 0.0,
                    size=size,
                    ok=result.ok,
                    status=result.status,
                    filled_size=result.filled_size,
                    avg_px=result.avg_px,
                    oid=result.oid,
                    message=result.message,
                )
            except Exception:
                log.exception("could not write order_log")
        if session.connected:
            with contextlib.suppress(Exception):  # socket may have closed meanwhile
                await send(
                    ws,
                    session,
                    {
                        "type": "order",
                        "ok": result.ok,
                        "status": result.status,
                        "message": result.message,
                        "coin": req.coin,
                        "kind": req.kind,
                        "fraction": fraction,
                    },
                )
            session.wake()

    async def sender(ws: WebSocket, session: ClientSession) -> None:
        loop = asyncio.get_running_loop()
        last_sent = float("-inf")
        while True:
            # wake on a change, or after heartbeat_s with no change (heartbeat frame)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session.dirty.wait(), timeout=heartbeat_s)
            delay = next_send_delay(last_sent, loop.time(), min_interval_s)
            if delay > 0:
                await asyncio.sleep(delay)  # coalesce bursts: latest state wins
            session.dirty.clear()
            session.seq += 1
            ui = session.flow.ui(loop.time(), trading=trading_on())
            if ui.get("mode") == "end" and sessions is not None:
                ui["summary"] = sessions.snapshot().to_ui()  # live numbers for "End session?"
            frame = build_hud_frame(
                state,
                index=session.index,
                seq=session.seq,
                ui=ui,
                lock_coin=session.flow.coin,
                trading=trading_info(),
                inbox=inbox.summary() if inbox is not None else None,
                voice=session.voice_overlay,
                glyphs=glyphs,
            )
            session.index = frame["view"]["index"]  # clamp / follow the locked coin
            await send(ws, session, frame)
            last_sent = loop.time()

    def flow_ctx(session: ClientSession, position: Position | None, now: float) -> dict[str, Any]:
        return {
            "position": position,
            "now": now,
            "trading": trading_on(),
            "size_decimals": adapter.size_decimals(position.coin) if position else None,
            "watch": state.watch,
            "precheck": precheck,
            "ref_px": ref_price(session.flow.coin) if session.flow.coin else None,
        }

    def apply_outcome(ws: WebSocket | None, session: ClientSession, out: Any, now: float) -> None:
        """Side effects an ActionFlow Outcome asks for (shared by taps, voice, pending voice)."""
        if out.timer_s is not None:
            schedule(session, out.timer_s, out.token)
        if out.execute is not None:
            target = ws if ws is not None else session_ws[session]
            via = bool(out.extra.get("via_alert"))
            spawn(session, run_order(target, session, out.execute, via), "order")
        if out.blocked and sessions is not None:
            sessions.tracker.record_block()
        if out.end_session and sessions is not None:
            spawn(session, end_session(session), "end-session")
        if out.post_summary:
            spawn(session, post_summary(session), "journal")
        if out.fetch_stop is not None:
            spawn(session, load_stop(session, out.fetch_stop), "stop-lookup")
        if out.snooze is not None and on_snooze is not None:
            on_snooze(out.snooze[0], out.snooze[1], now)
        if out.send_reply is not None:
            target_ws = ws if ws is not None else session_ws[session]
            spawn(session, deliver_reply(target_ws, session, out.send_reply), "reply-send")

    session_ws: dict[ClientSession, WebSocket] = {}

    def show_voice(session: ClientSession, overlay: dict[str, Any] | None, hold_s: float) -> None:
        """Put the voice line on this client's screen; clear it after hold_s (0 = keep)."""
        session.voice_overlay = overlay
        session.voice_token += 1
        token = session.voice_token
        session.wake()
        if hold_s > 0:

            def clear() -> None:
                if session.voice_token == token:
                    session.voice_overlay = None
                    session.wake()

            session.timers.append(asyncio.get_running_loop().call_later(hold_s, clear))

    async def run_voice(ws: WebSocket, session: ClientSession, pcm: bytes) -> None:
        loop = asyncio.get_running_loop()
        if audio_override is not None and (substitute := audio_override()) is not None:
            pcm = substitute  # demo backend: synthesized speech instead of the room microphone
        assert voice is not None
        try:
            text = await voice.transcribe(pcm)
        except RuntimeError as exc:
            show_voice(session, {"state": "error", "text": "", "note": str(exc)[:60]}, 3.0)
            return
        parsed = parse_voice(text)
        out = session.flow.voice(
            parsed.command,
            parsed.heard,
            **flow_ctx(session, selected_position(session), loop.time()),
        )
        apply_outcome(ws, session, out, loop.time())
        note = (
            out.note if parsed.command is not None or session.flow.mode == "reply" else parsed.error
        )
        hold = VOICE_OVERLAY_S
        if out.show_status and status_line is not None:
            note, hold = status_line(), STATUS_OVERLAY_S
        show_voice(session, {"state": "heard", "text": parsed.heard, "note": note}, hold)
        label = parsed.command.label if parsed.command else None
        log.info(  # the command, never free dictation text
            "voice from %s: %s (%.1fs audio) -> mode=%s%s",
            session.client,
            label or "no command",
            len(pcm) / BYTES_PER_SECOND,
            session.flow.mode,
            f" ({note})" if note else "",
        )
        if session.connected:
            with contextlib.suppress(Exception):
                await send(ws, session, {
                    "type": "voice", "state": "heard", "text": parsed.heard, "command": label,
                    "ok": out.ok and parsed.error is None, "note": note,
                })  # fmt: skip

    async def handle_voice(ws: WebSocket, session: ClientSession, msg: VoiceMessage) -> None:
        if msg.action == "start":
            if voice is None or voice.status in ("off", "error"):
                reason = voice.detail if voice is not None else "voice not configured"
                show_voice(session, {"state": "error", "text": "", "note": reason[:60]}, 3.0)
                await send(ws, session, {"type": "voice", "state": "error", "note": reason})
                return
            session.voice_buf = bytearray()
            show_voice(session, {"state": "listening", "text": "", "note": None}, 0)
            await send(ws, session, {"type": "voice", "state": "listening"})
        elif msg.action == "cancel":
            session.voice_buf = None
            show_voice(session, None, 0)
        else:  # stop
            await finish_voice(ws, session)

    async def finish_voice(ws: WebSocket, session: ClientSession) -> None:
        if session.voice_buf is None:
            return
        pcm, session.voice_buf = bytes(session.voice_buf), None
        show_voice(session, {"state": "transcribing", "text": "", "note": None}, 0)
        spawn(session, run_voice(ws, session, pcm), "voice")

    async def handle_audio(ws: WebSocket, session: ClientSession, chunk: bytes) -> None:
        if session.voice_buf is None:
            return  # audio outside push-to-talk is ignored (and never stored)
        session.voice_buf.extend(chunk[: MAX_VOICE_BYTES - len(session.voice_buf)])
        if len(session.voice_buf) >= MAX_VOICE_BYTES:
            await finish_voice(ws, session)  # held too long: transcribe what we have

    async def receiver(ws: WebSocket, session: ClientSession) -> None:
        loop = asyncio.get_running_loop()
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("bytes") is not None:
                await handle_audio(ws, session, message["bytes"])
                continue
            text = message.get("text") or ""
            try:
                data = json.loads(text)
                if isinstance(data, dict) and data.get("type") == "voice":
                    await handle_voice(ws, session, VoiceMessage.model_validate(data))
                    continue
                if isinstance(data, dict) and data.get("type") == "rendered":
                    rendered = RenderedMessage.model_validate(data)
                    if latency is not None:
                        latency.add_ack(rendered.src_ms)
                    continue
                intent = IntentMessage.model_validate(data)
            except (ValueError, ValidationError):
                await send(
                    ws, session, {"type": "error", "error": "bad_message", "raw": text[:200]}
                )
                continue
            position = selected_position(session)
            out = session.flow.handle(intent.action, **flow_ctx(session, position, loop.time()))
            count = len(state.positions)
            if out.nav is not None:
                session.index = apply_nav(session.index, count, out.nav)
            apply_outcome(ws, session, out, loop.time())
            log.info(
                "intent %s from %s -> mode=%s index=%d/%d%s",
                intent.action,
                session.client,
                session.flow.mode,
                session.index,
                count,
                f" ({out.note})" if out.note else "",
            )
            if store is not None:
                await asyncio.to_thread(store.log_intent, intent.action, session.client)
            ack: dict[str, Any] = {
                "type": "ack",
                "action": intent.action,
                "ok": out.ok,
                "mode": session.flow.mode,
                "view": {"index": session.index, "count": count},
            }
            if out.note:
                ack["note"] = out.note
            await send(ws, session, ack)
            session.wake()

    @router.websocket("/ws")
    async def hud_socket(ws: WebSocket) -> None:
        code = ws.query_params.get("code", "")
        await ws.accept()
        if not secrets.compare_digest(code.encode(), expected):
            # accept-then-close so the client sees a reason instead of a bare handshake failure
            await ws.close(code=CLOSE_BAD_PAIRING_CODE, reason="bad pairing code")
            log.warning("WS rejected: bad pairing code from %s", ws.client)
            return
        client = f"{ws.client.host}:{ws.client.port}" if ws.client else "unknown"
        session = ClientSession(client)
        await send(
            ws,
            session,
            {
                "type": "hello",
                "v": PROTOCOL_VERSION,
                "network": "testnet",
                "min_interval_ms": int(min_interval_s * 1000),
                "heartbeat_ms": int(heartbeat_s * 1000),
                "trading": trading_info(),
                "glyphs": glyphs,
            },
        )
        state.add_listener(session.wake)
        hub.sessions.add(session)
        session_ws[session] = ws
        waiting = hub.take_pending_draft(asyncio.get_running_loop().time())
        alert = active_alert() if active_alert is not None else None
        if alert is not None:
            # Away mode: an alert that fired (and was DMed) while nobody wore the glasses is still
            # unresolved -> open straight on its RISK screen; a waiting draft comes after it
            session.flow.raise_alert(alert)
            if waiting is not None:
                session.flow.pending_draft = waiting
            log.info("WS client opens on the RISK screen: %s %s", alert.coin, alert.side)
        elif waiting is not None:
            session.flow.raise_draft(*waiting)
        session.wake()  # first hud frame immediately
        log.info("WS client connected: %s (%d total)", client, hub.count)
        send_task = asyncio.create_task(sender(ws, session), name=f"hud-sender-{client}")
        send_task.add_done_callback(_log_task_failure)
        try:
            await receiver(ws, session)
        except WebSocketDisconnect:
            pass
        finally:
            session.connected = False
            session.cancel_timers()
            state.remove_listener(session.wake)
            hub.sessions.discard(session)
            session_ws.pop(session, None)
            session.voice_buf = None  # an unfinished push-to-talk is dropped, never transcribed
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
            log.info("WS client disconnected: %s (%d left)", client, hub.count)

    return router
