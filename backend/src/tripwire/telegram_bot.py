"""Telegram Business bot skeleton (python-telegram-bot, Bot API only — no userbot / MTProto).

Day 1 behaviour:
  /start                    -> short intro
  business_connection       -> log metadata + upsert into SQLite (who connected, enabled, can reply)
  business_message          -> log metadata ONLY (ids, length) — text is never logged or stored
  forwarded private message -> echo it back (placeholder for "forward a message to the copilot")

Runs inside the FastAPI process via long polling (no public webhook needed). With no
TELEGRAM_BOT_TOKEN, or if Telegram rejects the token / is unreachable, the bot is skipped and the
rest of the backend keeps running; `status` reports why.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from telegram import Update
from telegram.error import InvalidToken, TelegramError
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

BotStatus = Literal["disabled", "running", "error"]

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


def build_application(token: str, store: Store | None) -> Application:  # type: ignore[type-arg]
    app = Application.builder().token(token).build()
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


class TelegramBot:
    def __init__(self, token: str, store: Store | None) -> None:
        self.token = token.strip()
        self.store = store
        self.status: BotStatus = "disabled"
        self.detail = "not started" if self.token else "TELEGRAM_BOT_TOKEN is empty"
        self._app: Application | None = None  # type: ignore[type-arg]

    async def start(self) -> None:
        if not self.token:
            log.info("Telegram bot skipped: TELEGRAM_BOT_TOKEN is empty")
            return
        app = build_application(self.token, self.store)
        try:
            await app.initialize()  # calls getMe -> validates the token
            await app.start()
            assert app.updater is not None
            await app.updater.start_polling(
                allowed_updates=ALLOWED_UPDATES, drop_pending_updates=True
            )
        except (InvalidToken, TelegramError, OSError) as exc:
            self.status, self.detail = "error", f"{type(exc).__name__}: {exc}"
            log.error("Telegram bot skipped: %s", self.detail)
            await self._safe_shutdown(app)
            return
        self._app = app
        self.status, self.detail = "running", f"@{app.bot.username}"
        log.info("Telegram bot running as @%s", app.bot.username)

    async def stop(self) -> None:
        if self._app is not None:
            await self._safe_shutdown(self._app)
            self._app = None

    @staticmethod
    async def _safe_shutdown(app: Application) -> None:  # type: ignore[type-arg]
        try:
            if app.updater is not None and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception as exc:  # shutdown must never crash the backend
            log.debug("Telegram shutdown: %s", exc)
