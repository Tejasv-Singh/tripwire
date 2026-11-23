"""`/ws?code=<PAIRING_CODE>` — the glasses' only connection to the backend.

Per connection:
  * a sender task pushes a "hud" frame whenever the market state (or this client's selection)
    changes — at most one frame every `min_interval_s` (500 ms) — plus a heartbeat frame every
    `heartbeat_s` so the glasses can show feed age / detect a dead link;
  * a receiver loop accepts intents {"type":"intent","action":"next"|"prev"|"actions"}.
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

from tripwire.db import Store
from tripwire.hud import PROTOCOL_VERSION, apply_nav, build_hud_frame, next_send_delay
from tripwire.state import MarketState

log = logging.getLogger(__name__)

CLOSE_BAD_PAIRING_CODE = 4401
MIN_INTERVAL_S = 0.5
HEARTBEAT_S = 5.0


class IntentMessage(BaseModel):
    type: Literal["intent"]
    action: Literal["next", "prev", "actions"]


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.warning("%s stopped: %s: %s", task.get_name(), type(exc).__name__, exc)


class ClientSession:
    """State for one connected glasses client."""

    def __init__(self, client: str) -> None:
        self.client = client
        self.index = 0
        self.seq = 0
        self.dirty = asyncio.Event()
        self.send_lock = asyncio.Lock()

    def wake(self) -> None:
        self.dirty.set()


def make_ws_router(
    *,
    state: MarketState,
    pairing_code: str,
    store: Store | None,
    min_interval_s: float = MIN_INTERVAL_S,
    heartbeat_s: float = HEARTBEAT_S,
) -> APIRouter:
    router = APIRouter()
    expected = pairing_code.encode()

    async def send(ws: WebSocket, session: ClientSession, payload: dict[str, Any]) -> None:
        async with session.send_lock:  # sender task and receiver loop both send
            await ws.send_json(payload)

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
            frame = build_hud_frame(state, index=session.index, seq=session.seq)
            session.index = frame["view"]["index"]  # clamp if positions disappeared
            await send(ws, session, frame)
            last_sent = loop.time()

    async def receiver(ws: WebSocket, session: ClientSession) -> None:
        async for text in ws.iter_text():
            try:
                intent = IntentMessage.model_validate_json(text)
            except ValidationError:
                await send(
                    ws, session, {"type": "error", "error": "bad_message", "raw": text[:200]}
                )
                continue
            count = len(state.positions)
            session.index = apply_nav(session.index, count, intent.action)
            log.info(
                "intent %s from %s -> index %d/%d",
                intent.action,
                session.client,
                session.index,
                count,
            )
            if store is not None:
                await asyncio.to_thread(store.log_intent, intent.action, session.client)
            ack: dict[str, Any] = {
                "type": "ack",
                "action": intent.action,
                "ok": True,
                "view": {"index": session.index, "count": count},
            }
            if intent.action == "actions":
                ack["note"] = "actions menu not implemented yet (Day 1 logs only)"
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
            state.remove_listener(session.wake)
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
            log.info("WS client disconnected: %s (%d left)", client, state.listener_count)

    return router
