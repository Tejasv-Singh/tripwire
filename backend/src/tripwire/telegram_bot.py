"""Telegram Business bot skeleton (python-telegram-bot, Bot API only — no userbot / MTProto).

Day 1 behaviour:
  /start                    -> short intro + the sender's numeric user id (for TELEGRAM_OWNER_ID)
  /kill, /unkill            -> owner only: engage / release the policy kill switch (policy.py)
  /threshold [pct]          -> owner only: show / set the liquidation-alert threshold (risk.py)
  send_owner(text)          -> DM the owner (risk alerts when no glasses are connected)
  business_connection       -> log metadata + upsert into SQLite (who connected, enabled, can reply)
  business_message          -> log metadata ONLY (ids, length) — text is never logged or stored;
                               the first 30 characters go to the in-memory Inbox for the HUD line
  /status                   -> owner only: positions + uPnL, trading mode, kill switch, alerts
  /summary                  -> owner only: the current session so far (session.py)
  my_chat_member            -> when the bot is added to a channel / group, DM the owner its id
                               (that is the JOURNAL_CHAT_ID to put in .env)
  send_to_chat(id, text)    -> the end-of-session journal post (bot must be admin of a channel)
  forwarded / typed text    -> owner only: parse into an order draft (drafts.py), run the policy
                               guard, show it on the glasses for the two-tap confirm

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
import contextlib
import logging
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from telegram import Update
from telegram.error import InvalidToken, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tripwire.db import Store
from tripwire.drafts import DraftError, OrderDraft, parse_draft
from tripwire.inbox import Inbox
from tripwire.policy import Decision, KillSwitch
from tripwire.risk import AlertService

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
    Update.MY_CHAT_MEMBER,  # added to a channel -> tell the owner its id (journal setup)
]

START_TEXT = (
    "Tripwire — trading-session risk co-pilot for Even Realities G2 glasses "
    "(Hyperliquid TESTNET).\n\n"
    "• Connect me to your Telegram Business account (Settings → Business → Chatbots) to let me "
    "see only the chats you choose.\n"
    "• Forward me a message and I'll echo it back (Day 1 placeholder).\n\n"
    "I never store message text. Testnet only. Not financial advice."
)


@dataclass(slots=True)
class BotServices:
    """What the bot's handlers may touch. `owner_id` gates every command that reveals positions
    or changes behaviour; everyone else only gets /start and the forward echo."""

    owner_id: int | None = None
    kill_switch: KillSwitch | None = None
    alerts: AlertService | None = None
    # forwarded-message drafts (item 6)
    known_coins: Callable[[], set[str] | None] | None = None
    check_draft: Callable[[OrderDraft], Decision] | None = None
    push_draft: Callable[[OrderDraft, Decision], int] | None = None
    default_notional: float = 100.0
    default_leverage: float = 1.0
    inbox: Inbox | None = None  # business messages -> HUD line (memory only)
    status_text: Callable[[], str] | None = None  # /status
    summary_text: Callable[[], str] | None = None  # /summary
    journal_chat_id: int | None = None


def is_owner(update: Update, services: BotServices | None) -> bool:
    user = update.effective_user
    return bool(
        services is not None
        and services.owner_id is not None
        and user is not None
        and user.id == services.owner_id
    )


def make_start_handler(services: BotServices | None) -> CommandHandler[Any, None]:
    async def on_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if msg is None:
            return
        text = START_TEXT
        if user is not None:
            if is_owner(update, services):
                text += "\n\nYou are the owner: alerts come here; /status, /kill and /unkill work."
            else:
                text += (
                    f"\n\nYour Telegram user id is {user.id}. To receive risk alerts and use "
                    f"/status, /kill and /unkill, put TELEGRAM_OWNER_ID={user.id} in the "
                    "backend's .env and restart it."
                )
        await msg.reply_text(text)

    return CommandHandler("start", on_start, filters=filters.UpdateType.MESSAGE)


def make_kill_handlers(services: BotServices | None) -> list[CommandHandler[Any, None]]:
    async def on_kill(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        if not is_owner(update, services) or services is None or services.kill_switch is None:
            await msg.reply_text("Not authorized.")
            return
        await asyncio.to_thread(services.kill_switch.engage)
        await msg.reply_text(
            "KILL SWITCH ON. Only reduce-only orders (closes, tighter stop-losses) until /unkill."
        )

    async def on_unkill(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        if not is_owner(update, services) or services is None or services.kill_switch is None:
            await msg.reply_text("Not authorized.")
            return
        released = await asyncio.to_thread(services.kill_switch.release)
        await msg.reply_text(
            "Kill switch released."
            if released
            else "Telegram flag cleared, but KILL_SWITCH=true in .env keeps it ON. Edit .env and "
            "restart to release it."
        )

    only_messages = filters.UpdateType.MESSAGE
    return [
        CommandHandler("kill", on_kill, filters=only_messages),
        CommandHandler("unkill", on_unkill, filters=only_messages),
    ]


def _draft_filter() -> Any:
    return (
        filters.UpdateType.MESSAGE
        & filters.ChatType.PRIVATE
        & (filters.TEXT | filters.CAPTION)
        & ~filters.COMMAND
    )


def make_threshold_handler(services: BotServices | None) -> CommandHandler[Any, None]:
    async def on_threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        if not is_owner(update, services) or services is None or services.alerts is None:
            await msg.reply_text("Not authorized.")
            return
        engine = services.alerts.engine
        args = list(getattr(ctx, "args", None) or [])
        if not args:
            await msg.reply_text(
                f"Liquidation alert at {engine.threshold_pct:g}% from mark "
                f"(re-arms at {engine.threshold_pct + engine.hysteresis_pct:g}%). "
                "Change it with /threshold 8"
            )
            return
        try:
            pct = float(args[0].rstrip("%"))
        except ValueError:
            pct = -1.0
        if not 0 < pct <= 100:
            await msg.reply_text("Give a percentage between 0 and 100, e.g. /threshold 8")
            return
        await asyncio.to_thread(services.alerts.set_threshold, pct)
        await msg.reply_text(f"Alert threshold set to {pct:g}% (all positions re-armed).")

    return CommandHandler("threshold", on_threshold, filters=filters.UpdateType.MESSAGE)


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


def make_business_message_handler(services: BotServices | None) -> MessageHandler[Any, None]:
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
        sender = msg.from_user
        own = sender is not None and services is not None and sender.id == services.owner_id
        new = update.business_message is not None
        if services is None or services.inbox is None or own or not new or not text:
            return
        name = "?"
        if sender is not None:
            name = f"@{sender.username}" if sender.username else (sender.first_name or "?")
        services.inbox.add(
            connection_id=str(msg.business_connection_id),
            chat_id=msg.chat_id,
            name=name,
            text=text,  # Inbox keeps only the first 30 characters, in memory
        )

    return MessageHandler(filters.UpdateType.BUSINESS_MESSAGES, on_business_message)


def make_summary_handler(services: BotServices | None) -> CommandHandler[Any, None]:
    async def on_summary(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        if not is_owner(update, services) or services is None or services.summary_text is None:
            await msg.reply_text("Not authorized.")
            return
        await msg.reply_text(services.summary_text())

    return CommandHandler("summary", on_summary, filters=filters.UpdateType.MESSAGE)


def make_member_handler(services: BotServices | None) -> ChatMemberHandler[Any, None]:
    """Added to a channel / group: tell the owner its id (for JOURNAL_CHAT_ID). Ids only."""

    async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        change = update.my_chat_member
        if change is None or services is None or not services.owner_id:
            return
        chat, status = change.chat, change.new_chat_member.status
        log.info("bot membership in chat %s (%s): %s", chat.id, chat.type, status)
        if status not in ("administrator", "member") or chat.type == "private":
            return
        admin = status == "administrator"
        hint = "" if admin else " Make me an ADMIN there so I can post the session journal."
        current = (
            " (this is the JOURNAL_CHAT_ID already set)"
            if chat.id == services.journal_chat_id
            else ""
        )
        with contextlib.suppress(TelegramError):
            await ctx.bot.send_message(
                chat_id=services.owner_id,
                text=f"I was added to {chat.type} '{chat.title}' as {status}. Its id is {chat.id}"
                f"{current}. For session journals put JOURNAL_CHAT_ID={chat.id} in .env and "
                f"restart.{hint}",
            )

    return ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)


def make_status_handler(services: BotServices | None) -> CommandHandler[Any, None]:
    async def on_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        if not is_owner(update, services) or services is None or services.status_text is None:
            await msg.reply_text("Not authorized.")
            return
        await msg.reply_text(services.status_text())

    return CommandHandler("status", on_status, filters=filters.UpdateType.MESSAGE)


DRAFT_HEADER = "FORWARDED - your instruction, not advice"


def make_draft_handler(services: BotServices | None) -> MessageHandler[Any, None]:
    """Forwarded (or typed) text from the owner -> order draft -> policy -> glasses."""

    async def on_draft_text(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.message
        if msg is None:
            return
        text = msg.text or msg.caption or ""
        log.info(  # metadata only: never the text
            "draft candidate chat=%s msg=%s forwarded=%s len=%d",
            msg.chat_id,
            msg.message_id,
            msg.forward_origin is not None,
            len(text),
        )
        if not is_owner(update, services) or services is None:
            await msg.reply_text("Order drafts from forwarded messages are only for the bot owner.")
            return
        try:
            draft = parse_draft(
                text,
                default_notional=services.default_notional,
                default_leverage=services.default_leverage,
                known_coins=services.known_coins() if services.known_coins else None,
            )
        except DraftError as exc:
            await msg.reply_text(f"Not an order I can draft: {exc}.")
            return
        decision = services.check_draft(draft) if services.check_draft else None
        shown = services.push_draft(draft, decision) if services.push_draft and decision else 0
        lines = [DRAFT_HEADER, draft.summary()]
        if draft.defaults:
            lines.append("Defaults used: " + ", ".join(draft.defaults))
        if decision is None:
            lines.append("Policy: not checked (backend not ready)")
        elif decision.allowed:
            lines.append("Policy: OK - " + decision.reason)
        else:
            lines.append("BLOCKED by policy: " + decision.reason)
        lines.append(
            "On your glasses now: two taps to confirm."
            if shown and decision is not None and decision.allowed
            else "Shown on your glasses."
            if shown
            else "No glasses connected: it will show when they connect (5 min)."
        )
        await msg.reply_text("\n".join(lines))

    return MessageHandler(_draft_filter(), on_draft_text)


def on_polling_error(exc: TelegramError) -> None:
    """getUpdates failed while running: almost always Telegram dropping the long-poll connection
    (httpx.ReadError). PTB retries on its own; log ONE line instead of PTB's default ERROR with a
    40-line traceback. Must be a plain function: PTB refuses coroutine error callbacks."""
    log.warning(
        "Telegram polling hiccup (%s: %s) - reconnecting automatically", type(exc).__name__, exc
    )


def build_application(
    token: str, store: Store | None, services: BotServices | None = None
) -> Application:  # type: ignore[type-arg]
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(CONNECT_TIMEOUT_S)
        .read_timeout(READ_TIMEOUT_S)
        .write_timeout(READ_TIMEOUT_S)
        .build()
    )
    app.add_handler(make_start_handler(services))
    for handler in make_kill_handlers(services):
        app.add_handler(handler)
    app.add_handler(make_threshold_handler(services))
    app.add_handler(make_business_connection_handler(store))
    app.add_handler(make_business_message_handler(services))
    app.add_handler(make_status_handler(services))
    app.add_handler(make_summary_handler(services))
    app.add_handler(make_member_handler(services))
    app.add_handler(make_draft_handler(services))
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
        self.owner_id: int | None = None  # DM target for send_owner()
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

    async def send_owner(self, text: str) -> bool:
        """DM the owner. False if the bot is not running, no owner is set, or Telegram refused."""
        if self.status != "running" or self._app is None or not self.owner_id:
            return False
        try:
            await self._app.bot.send_message(chat_id=self.owner_id, text=text)
        except TelegramError as exc:
            log.warning("Telegram DM to owner failed: %s: %s", type(exc).__name__, exc)
            return False
        return True

    async def send_to_chat(self, chat_id: int, text: str) -> tuple[bool, str]:
        """Post to a channel / group (the session journal). (ok, short reason)."""
        if self.status != "running" or self._app is None:
            return False, "Telegram bot not running"
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
        except TelegramError as exc:
            log.warning("journal post failed: %s: %s", type(exc).__name__, exc)
            return False, f"{type(exc).__name__}: {exc}"[:80]
        return True, "posted to the journal"

    async def send_business_reply(self, connection_id: str, chat_id: int, text: str) -> bool:
        """Answer in a business chat AS the user (needs the "reply to messages" right)."""
        if self.status != "running" or self._app is None:
            return False
        try:
            await self._app.bot.send_message(
                chat_id=chat_id, text=text, business_connection_id=connection_id
            )
        except TelegramError as exc:
            log.warning("business reply failed: %s: %s", type(exc).__name__, exc)
            return False
        return True

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
