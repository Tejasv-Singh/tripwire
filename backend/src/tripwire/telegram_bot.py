"""Telegram Business bot skeleton (python-telegram-bot, Bot API only — no userbot / MTProto).

Day 1 behaviour:
  /start                    -> short intro
  business_connection       -> log metadata + upsert into SQLite (who connected, enabled, can reply)
  business_message          -> log metadata ONLY (ids, length) — text is never logged or stored
  forwarded private message -> echo it back (placeholder for "forward a message to the copilot")

Runs inside the FastAPI process via long polling (no public webhook needed). Startup never
blocks or crashes the backend:
  * no TELEGRAM_BOT_TOKEN         -> status "disabled", nothing runs
  * Telegram rejects the token    -> status "error", no retries (fix .env, restart)
  * timeout / network / 429 flood -> status "starting", retried in the background: waits the
                                     server's retry_after on 429, else backs off 5 s -> 300 s
`status` / `detail` are exposed on /healthz.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Literal

from telegram import Update
from telegram.error import InvalidToken, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tripwire.db import Store

log = logging.getLogger(__name__)

BotStatus = Literal["disabled", "starting", "running", "error"]

RETRY_BASE_S = 5.0  # first backoff after a timeout / network error
RETRY_MAX_S = 300.0  # backoff cap
# Per-request timeouts for normal Bot API calls (PTB defaults are 5 s; too tight on slow links).
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 20.0

ALLOWED_UPDATES = [
    Update.MESSAGE,
    Update.BUSINESS_CONNECTION,
    Update.BUSINESS_MESSAGE,
    Update.EDITED_BUSINESS_MESSAGE,
    Update.DELETED_BUSINESS_MESSAGES,
]

START_TEXT = (
    "Tripwire — trading-session risk co-pilot for Even Realities G2 glasses "
    "(Hyperliquid TESTNET).\n\n"
    "• Connect me to your Telegram Business account (Settings → Business → Chatbots) to let me "
    "see only the chats you choose.\n"
    "• Forward me a message and I'll echo it back (Day 1 placeholder).\n\n"
    "I never store message text. Testnet only. Not financial advice."
)


async def on_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(START_TEXT)


def make_business_connection_handler(
    store: Store | None,
) -> BusinessConnectionHandler[Any, None]:
    async def on_business_connection(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        bc = update.business_connection
        if bc is None:
            return
        can_reply = bool(bc.rights and bc.rights.can_reply)
        log.info(
            "business_connection id=%s user_id=%s enabled=%s can_reply=%s",
            bc.id,
            bc.user.id,
            bc.is_enabled,
            can_reply,
        )
        if store is not None:
            await asyncio.to_thread(
                store.upsert_business_connection,
                connection_id=bc.id,
                user_id=bc.user.id,
                user_chat_id=bc.user_chat_id,
                is_enabled=bc.is_enabled,
                can_reply=can_reply,
                connected_at=int(bc.date.timestamp()),
            )

    return BusinessConnectionHandler(on_business_connection)


async def on_business_message(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.business_message or update.edited_business_message
    if msg is None:
        return
    text = msg.text or msg.caption or ""
    # Metadata only. Do NOT log `text` — privacy rule: no message retention.
    log.info(
        "business_message conn=%s chat=%s msg=%s edited=%s has_text=%s len=%d",
        msg.business_connection_id,
        msg.chat_id,
        msg.message_id,
        update.edited_business_message is not None,
        bool(text),
        len(text),
    )


async def on_forwarded(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None:
        return
    text = msg.text or msg.caption
    log.info(
        "forwarded message chat=%s msg=%s len=%d", msg.chat_id, msg.message_id, len(text or "")
    )
    await msg.reply_text(f"Echo: {text}" if text else "Echo: (forwarded message without text)")


def on_polling_error(exc: TelegramError) -> None:
    """getUpdates failed while running: almost always Telegram dropping the long-poll connection
    (httpx.ReadError). PTB retries on its own; log ONE line instead of PTB's default ERROR with a
    40-line traceback. Must be a plain function: PTB refuses coroutine error callbacks."""
    log.warning(
        "Telegram polling hiccup (%s: %s) - reconnecting automatically", type(exc).__name__, exc
    )


def build_application(token: str, store: Store | None) -> Application:  # type: ignore[type-arg]
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(CONNECT_TIMEOUT_S)
        .read_timeout(READ_TIMEOUT_S)
        .write_timeout(READ_TIMEOUT_S)
        .build()
    )
    app.add_handler(CommandHandler("start", on_start, filters=filters.UpdateType.MESSAGE))
    app.add_handler(make_business_connection_handler(store))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGES, on_business_message))
    app.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & filters.FORWARDED,
            on_forwarded,
        )
    )
    return app


def retry_delay(exc: BaseException, attempt: int) -> float:
    """Seconds to wait before the next startup attempt (attempt is 1-based).

    Exponential backoff (5 s doubling, capped at 300 s). On a 429 the wait is at least what
    Telegram asked for (+1 s) — but never less than the backoff, because a request that times out
    still reaches Telegram and re-arms the flood window; fixed 33 s retries could stay stuck."""
    backoff = float(min(RETRY_BASE_S * 2 ** (attempt - 1), RETRY_MAX_S))
    if isinstance(exc, RetryAfter):
        with warnings.catch_warnings():  # PTB 22.2+ warns that int will become timedelta
            warnings.simplefilter("ignore")
            wait = exc.retry_after
        seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
        return max(seconds + 1.0, backoff)
    return backoff


AppFactory = Callable[[str, Store | None], Any]
Sleep = Callable[[float], Awaitable[None]]


class TelegramBot:
    def __init__(
        self,
        token: str,
        store: Store | None,
        *,
        app_factory: AppFactory = build_application,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.token = token.strip()
        self.store = store
        self.status: BotStatus = "disabled"
        self.detail = "not started" if self.token else "TELEGRAM_BOT_TOKEN is empty"
        self.attempts = 0
        self._app: Any = None
        self._task: asyncio.Task[None] | None = None
        self._factory = app_factory
        self._sleep = sleep

    async def start(self) -> None:
        """Non-blocking: connects in a background task so backend startup is never held up."""
        if not self.token:
            log.info("Telegram bot skipped: TELEGRAM_BOT_TOKEN is empty")
            return
        self.status, self.detail = "starting", "connecting to Telegram"
        self._task = asyncio.create_task(self._run(), name="telegram-bot")

    async def wait_started(self) -> None:
        """Await the background startup loop (tests / scripts)."""
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        while True:
            self.attempts += 1
            app = self._factory(self.token, self.store)
            try:
                await app.initialize()  # getMe -> validates the token
                await app.start()
                await app.updater.start_polling(
                    allowed_updates=ALLOWED_UPDATES,
                    drop_pending_updates=True,
                    error_callback=on_polling_error,
                )
            except asyncio.CancelledError:
                await self._safe_shutdown(app)
                raise
            except InvalidToken as exc:  # permanent: retrying can't help
                self.status = "error"
                self.detail = f"InvalidToken: {exc} - fix TELEGRAM_BOT_TOKEN in .env and restart"
                log.error("Telegram bot disabled: %s", self.detail)
                await self._safe_shutdown(app)
                return
            except (TelegramError, OSError) as exc:  # transient: timeout, network, 429, 409
                delay = retry_delay(exc, self.attempts)
                reason = f"{type(exc).__name__}: {exc}"
                self.status = "starting"
                self.detail = f"attempt {self.attempts} failed ({reason}); retry in {delay:.0f}s"
                log.warning("Telegram bot not up yet (%s) - retrying in %.0f s", reason, delay)
                await self._safe_shutdown(app)
                await self._sleep(delay)
                continue
            self._app = app
            self.status, self.detail = "running", f"@{app.bot.username}"
            log.info("Telegram bot running as @%s (attempt %d)", app.bot.username, self.attempts)
            return

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._app is not None:
            await self._safe_shutdown(self._app)
            self._app = None

    @staticmethod
    async def _safe_shutdown(app: Any) -> None:
        try:
            if app.updater is not None and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception as exc:  # shutdown must never crash the backend
            log.debug("Telegram shutdown: %s", exc)
