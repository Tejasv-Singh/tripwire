"""`/ws?code=<PAIRING_CODE>` — the glasses' only connection to the backend.

Per connection:
  * a sender task pushes a "hud" frame whenever the market state (or this client's screen)
    changes — at most one frame every `min_interval_s` (500 ms) — plus a heartbeat frame every
    `heartbeat_s` so the glasses can show feed age / detect a dead link;
  * a receiver loop accepts intents {"type":"intent","action":"next"|"prev"|"tap"|"back"} and
    feeds them to this client's ActionFlow (actions.py: menu -> two-tap confirm -> order);
  * a confirmed close runs as a background task (OrderExecutor, trading.py). It is deliberately
    NOT cancelled if the glasses disconnect: the order may already be on its way, and its result
    must still be logged.
Protocol reference: docs/ws-protocol.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from typing import Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from tripwire.actions import ActionFlow
from tripwire.db import Store
from tripwire.hl.positions import Position
from tripwire.hud import (
    PROTOCOL_VERSION,
    apply_nav,
    build_hud_frame,
    next_send_delay,
    resolve_index,
)
from tripwire.orders import Order, OrderResult, OrderService
from tripwire.policy import Decision, PolicyGuard
from tripwire.state import MarketState
from tripwire.trading import CloseRequest, OrderExecutor

log = logging.getLogger(__name__)

CLOSE_BAD_PAIRING_CODE = 4401
MIN_INTERVAL_S = 0.5
HEARTBEAT_S = 5.0


class IntentMessage(BaseModel):
    type: Literal["intent"]
    action: Literal["next", "prev", "tap", "back"]


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

    def wake(self) -> None:
        self.dirty.set()

    def cancel_timers(self) -> None:
        for handle in self.timers:
            handle.cancel()
        self.timers.clear()


def make_ws_router(
    *,
    state: MarketState,
    pairing_code: str,
    store: Store | None,
    executor: OrderExecutor,
    guard: PolicyGuard,
    orders: OrderService,
    min_interval_s: float = MIN_INTERVAL_S,
    heartbeat_s: float = HEARTBEAT_S,
) -> APIRouter:
    router = APIRouter()
    expected = pairing_code.encode()
    order_tasks: set[asyncio.Task[None]] = set()  # strong refs so running orders aren't GC'd

    def trading_on() -> bool:
        return executor.status != "off"

    def trading_info() -> dict[str, Any]:
        return {"enabled": trading_on(), "status": executor.status}

    def selected_position(session: ClientSession) -> Position | None:
        if not state.positions:
            return None
        p = state.positions[resolve_index(state, session.index, session.flow.coin)]
        if session.flow.coin is not None and p.coin != session.flow.coin:
            return None  # the coin the menu is about is no longer open
        return p

    def live_position(coin: str) -> Position | None:
        return next((p for p in state.positions if p.coin == coin), None)

    def precheck(order: Order) -> Decision:
        p = live_position(order.coin)
        return guard.check(order, position=p, mark=p.mark if p else state.marks.get(order.coin))

    async def send_close(order: Order) -> OrderResult:
        assert isinstance(order, CloseRequest)
        return await executor.close(order)

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

    async def run_order(ws: WebSocket, session: ClientSession, req: CloseRequest) -> None:
        try:
            p = live_position(req.coin)
            result = await orders.submit(  # policy guard, then the exchange
                req, position=p, mark=p.mark if p else None, send=send_close
            )
        except Exception as exc:  # an executor bug must not leave the screen stuck on "sending"
            log.exception("order task failed")
            result = OrderResult(False, "error", f"{type(exc).__name__}: {exc}"[:120])
        out = session.flow.finish(result)
        if out.timer_s is not None and session.connected:
            schedule(session, out.timer_s, out.token)
        log.info("order %s %s from %s -> %s", req.coin, req.fraction, session.client, result.status)
        if store is not None:
            try:
                await asyncio.to_thread(
                    store.log_order,
                    client=session.client,
                    coin=req.coin,
                    side=req.side,
                    fraction=req.fraction,
                    size=req.size,
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
                        "fraction": req.fraction,
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
            frame = build_hud_frame(
                state,
                index=session.index,
                seq=session.seq,
                ui=session.flow.ui(loop.time(), trading=trading_on()),
                lock_coin=session.flow.coin,
                trading=trading_info(),
            )
            session.index = frame["view"]["index"]  # clamp / follow the locked coin
            await send(ws, session, frame)
            last_sent = loop.time()

    async def receiver(ws: WebSocket, session: ClientSession) -> None:
        loop = asyncio.get_running_loop()
        async for text in ws.iter_text():
            try:
                intent = IntentMessage.model_validate_json(text)
            except ValidationError:
                await send(
                    ws, session, {"type": "error", "error": "bad_message", "raw": text[:200]}
                )
                continue
            position = selected_position(session)
            out = session.flow.handle(
                intent.action,
                position=position,
                now=loop.time(),
                trading=trading_on(),
                size_decimals=executor.size_decimals(position.coin) if position else None,
                watch=state.watch,
                precheck=precheck,
            )
            count = len(state.positions)
            if out.nav is not None:
                session.index = apply_nav(session.index, count, out.nav)
            if out.timer_s is not None:
                schedule(session, out.timer_s, out.token)
            if out.execute is not None:
                task = asyncio.create_task(
                    run_order(ws, session, out.execute), name=f"order-{session.client}"
                )
                order_tasks.add(task)
                task.add_done_callback(order_tasks.discard)
                task.add_done_callback(_log_task_failure)
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
            },
        )
        state.add_listener(session.wake)
        session.wake()  # first hud frame immediately
        log.info("WS client connected: %s (%d total)", client, state.listener_count)
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
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
            log.info("WS client disconnected: %s (%d left)", client, state.listener_count)

    return router
