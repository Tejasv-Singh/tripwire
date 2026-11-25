"""FastAPI app factory. Run with:  uvicorn tripwire.main:create_app --factory --port 8000

Startup order matters: the testnet guard runs first, before any network connection exists.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tripwire.config import Settings, assert_testnet
from tripwire.db import Store
from tripwire.drafts import OrderDraft
from tripwire.exchanges.base import ExchangeAdapter
from tripwire.exchanges.hyperliquid import HyperliquidAdapter
from tripwire.hl.feed import HyperliquidFeed
from tripwire.inbox import Inbox, ReplyTarget
from tripwire.latency import LatencyStats
from tripwire.orders import Order, OrderResult, OrderService
from tripwire.policy import Decision, KillSwitch, PolicyGuard, PolicyLimits
from tripwire.replies import ReplyDrafter
from tripwire.risk import AlertService, RiskEngine
from tripwire.session import SessionService
from tripwire.state import MarketState
from tripwire.telegram_bot import BotServices, TelegramBot, build_application
from tripwire.voice import VoiceEngine
from tripwire.ws import ClientHub, make_ws_router

log = logging.getLogger("tripwire")
KNOWN_COINS_MIN = 20  # allMids lists ~200 perps on testnet; fewer means "not loaded yet"
STATUS_MAX_POSITIONS = 20  # /status lists at most this many (Telegram messages max 4096 chars)


_SECRET_PATTERNS = [
    (re.compile(r"(code=)[^&\s\"']+"), r"\1***"),  # /ws?code=<PAIRING_CODE> in access logs
    (re.compile(r"(bot)\d+:[A-Za-z0-9_-]+"), r"\1***"),  # Telegram token inside API URLs
    (re.compile(r"sk-or-[A-Za-z0-9_-]+"), "sk-or-***"),  # OpenRouter key, if it ever shows up
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Rewrites log records so the pairing code / bot token never reach the console or files."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx (used by python-telegram-bot) logs every long-poll request at INFO; too noisy, and
    # those URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # uvicorn logs WS handshakes ("WebSocket /ws?code=..." [accepted]) on uvicorn.error and HTTP
    # requests on uvicorn.access; filters on a logger only see records logged *on* that logger.
    for name in ("uvicorn.error", "uvicorn.access", "httpx", "telegram", "tripwire"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, RedactSecretsFilter) for f in lg.filters):
            lg.addFilter(RedactSecretsFilter())


def format_status(
    state: MarketState,
    adapter: ExchangeAdapter,
    guard: PolicyGuard,
    alerts: AlertService,
    glasses: int | None = None,
) -> str:
    """The /status reply: mode, trading, policy, alerts (+ Away mode when `glasses` is given),
    then every position with uPnL."""
    mode = {
        "account": "own account",
        "watch": f"WATCH {state.account_short} (read-only)",
        "price": "price only (no account)",
    }[state.mode]
    if not adapter.can_trade:
        trading = f"off ({adapter.detail})"
    else:
        trading = (
            "DEMO (in-memory fake exchange)"
            if adapter.name == "fake"
            else "DRY RUN (signed, not sent)"
            if adapter.dry_run
            else "LIVE on testnet"
        )
    kill = "ON" if guard.kill_switch.engaged else "off"
    lines = [
        "Tripwire status (Hyperliquid TESTNET)",
        f"Positions: {mode}  |  Trading: {trading}",
        f"Kill switch: {kill}  |  Alerts at {alerts.engine.threshold_pct:g}% liq distance",
    ]
    if glasses is not None:
        lines.append(
            "Away mode: ON - no glasses connected, risk alerts come here"
            if glasses == 0
            else f"Away mode: off - glasses connected ({glasses})"
        )
    if alerts.episodes:
        lines.append(
            "Open alerts: "
            + ", ".join(
                f"{e.alert.coin} {e.alert.side} {e.alert.liq_dist_pct:.1f}% from liq"
                for e in alerts.episodes.values()
            )
        )
    positions = state.positions
    if not positions:
        lines.append("No open positions.")
    for p in positions[:STATUS_MAX_POSITIONS]:
        liq = (
            f"liq {p.liq_px:,.6g} ({p.liq_dist_pct:.1f}%)"
            if p.liq_px and p.liq_dist_pct
            else "no liq"
        )
        lines.append(
            f"{p.coin}-PERP {p.side} {p.size:g} @ {p.entry:,.6g}  mark {p.mark:,.6g}  "
            f"uPnL {p.upnl:+,.2f} ({p.upnl_pct:+.1f}%)  {liq}"
        )
    if len(positions) > STATUS_MAX_POSITIONS:
        lines.append(f"... and {len(positions) - STATUS_MAX_POSITIONS} more")
    if positions:
        lines.append(f"Total uPnL: {sum(p.upnl for p in positions):+,.2f} USD")
    return "\n".join(lines)


def status_line(state: MarketState, adapter: ExchangeAdapter, guard: PolicyGuard) -> str:
    """One line for the glasses (voice "status"): positions, uPnL, trading mode, kill switch."""
    n = len(state.positions)
    upnl = sum(p.upnl for p in state.positions)
    mode = (
        "WATCH" if state.watch
        else "off" if not adapter.can_trade
        else "DEMO" if adapter.name == "fake"
        else "DRY RUN" if adapter.dry_run
        else "LIVE"
    )  # fmt: skip
    kill = "  KILL ON" if guard.kill_switch.engaged else ""
    pos = f"{n} pos  uPnL {'-' if upnl < 0 else '+'}${abs(upnl):,.2f}" if n else "no positions"
    return f"{pos}  {mode}{kill}"


def create_app(
    settings: Settings | None = None,
    *,
    run_feed: bool = True,
    run_bot: bool = True,
    min_interval_s: float = 0.5,
    heartbeat_s: float = 1.0,
    make_adapter: Callable[[MarketState], ExchangeAdapter] | None = None,
    voice_engine: VoiceEngine | None = None,
    audio_override: Callable[[], bytes | None] | None = None,
) -> FastAPI:
    """`make_adapter` swaps the exchange (tests / demo: FakeAdapter). Default: Hyperliquid."""
    settings = settings or Settings()  # type: ignore[call-arg]  # values come from .env
    assert_testnet(settings.hl_api_url)  # refuse to start on anything but testnet
    configure_logging(settings.log_level)

    state = MarketState(ref_coin="ETH", account=settings.stream_address, watch=settings.watch_mode)
    store = Store(settings.resolved_db_path)
    kill_switch = KillSwitch(settings.kill_switch, store)
    if make_adapter is None:
        adapter: ExchangeAdapter = HyperliquidAdapter(
            state,
            base_url=settings.hl_api_url,
            account_address=settings.hl_account_address,
            agent_key=settings.hl_agent_key.get_secret_value(),
            watch=settings.watch_mode,
            dry_run=settings.dry_run,
            dry_run_log=settings.resolved_db_path.parent / "dry_run_orders.jsonl",
        )
    else:
        adapter = make_adapter(state)
    guard = PolicyGuard(
        PolicyLimits(
            max_open_notional_usd=settings.max_open_notional_usd,
            max_leverage=settings.max_leverage,
            daily_loss_limit_usd=settings.daily_loss_limit_usd,
        ),
        kill_switch,
        pnl_source=adapter.realized_pnl_today,
        watch_only=settings.watch_mode,
    )
    orders = OrderService(guard)
    bot_services = BotServices(owner_id=settings.telegram_owner_id, kill_switch=kill_switch)
    bot = TelegramBot(
        settings.telegram_bot_token.get_secret_value() if run_bot else "",
        store,
        app_factory=partial(build_application, services=bot_services),
    )
    feed = HyperliquidFeed(settings.hl_api_url, state) if run_feed else None
    hub = ClientHub()
    bot.owner_id = settings.telegram_owner_id
    alerts = AlertService(
        RiskEngine(
            threshold_pct=settings.alert_liq_pct,
            hysteresis_pct=settings.alert_hysteresis_pct,
            snooze_s=settings.alert_snooze_s,
        ),
        adapter,
        glasses=hub.raise_alert,
        notifier=bot,
        store=store,
    )
    bot_services.alerts = alerts

    def session_mode() -> str:
        base = {
            "account": "own account",
            "watch": f"WATCH {state.account_short} (read-only)",
            "price": "price only",
        }[state.mode]
        if adapter.name == "fake":
            return base + ", DEMO exchange"
        if adapter.can_trade and adapter.dry_run:
            return base + ", DRY RUN"
        return base

    sessions = SessionService(state, adapter, store=store, mode=session_mode)
    alerts.on_fire = lambda _alert: sessions.tracker.record_alert()

    def on_snooze(coin: str, side: str, now: float) -> None:
        alerts.engine.snooze(coin, side, now)
        alerts.close_episode(coin, side, "snoozed")
        sessions.tracker.record_snooze()

    def on_order_result(order: Order, result: OrderResult) -> None:
        """A close or stop that really changed the position ends its alert episode. A dry run,
        a block or a rejection doesn't: the risk is still there."""
        if order.kind in ("close", "stop") and result.status in ("filled", "placed", "resting"):
            alerts.close_episode(order.coin, order.side, f"acted on ({order.kind})")

    async def post_journal(text: str) -> tuple[bool, str]:
        if settings.journal_chat_id is None:
            return False, "JOURNAL_CHAT_ID not set in .env"
        return await bot.send_to_chat(settings.journal_chat_id, text)

    def known_coins() -> set[str] | None:
        """Perp names from allMids (spot pairs look like "@107" / "PURR/USDC"). None until the
        full list has arrived, so the parser doesn't reject coins it simply hasn't seen yet."""
        perps = {c.upper() for c in state.mids if c.isalnum()}
        return perps if len(perps) >= KNOWN_COINS_MIN else None

    def check_draft(draft: OrderDraft) -> Decision:
        p = next((x for x in state.positions if x.coin == draft.coin), None)
        mark = p.mark if p else (state.marks.get(draft.coin) or state.mids.get(draft.coin))
        return guard.check(draft.to_order(), position=p, mark=mark)

    def push_draft(draft: OrderDraft, decision: Decision) -> int:
        if not decision.allowed:
            sessions.tracker.record_block()
        return hub.push_draft(draft, decision, asyncio.get_running_loop().time())

    inbox = Inbox(on_change=hub.wake_all)
    replier = ReplyDrafter(
        settings.openrouter_api_key.get_secret_value(), settings.openrouter_model
    )

    async def send_reply(target: ReplyTarget, text: str) -> bool:
        return await bot.send_business_reply(target.connection_id, target.chat_id, text)

    latency = LatencyStats()

    def status_text() -> str:
        return format_status(state, adapter, guard, alerts, hub.count) + "\n" + latency.line()

    voice = voice_engine or VoiceEngine(
        enabled=settings.voice_enabled,
        model_size=settings.voice_model,
        model_dir=settings.resolved_db_path.parent / "models",
    )

    bot_services.inbox = inbox
    bot_services.status_text = status_text
    bot_services.summary_text = lambda: sessions.snapshot().text()
    bot_services.journal_chat_id = settings.journal_chat_id
    bot_services.known_coins = known_coins
    bot_services.check_draft = check_draft
    bot_services.push_draft = push_draft
    bot_services.default_notional = settings.draft_default_notional_usd
    bot_services.default_leverage = settings.draft_default_leverage

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(store.init)
        await asyncio.to_thread(kill_switch.load)
        if kill_switch.engaged:
            log.warning("Kill switch is ON (%s): only reduce-only orders", kill_switch.source)
        await asyncio.to_thread(alerts.load_threshold)
        feed_task = asyncio.create_task(feed.run(), name="hl-feed") if feed else None
        alert_task = asyncio.create_task(alerts.run(), name="risk-alerts")
        voice_task = asyncio.create_task(voice.warm_up(), name="voice-warm-up")
        session_task = asyncio.create_task(sessions.run(), name="session-sampling")
        warm_task = asyncio.create_task(adapter.start(), name="exchange-warm-up")
        await bot.start()
        log.info(
            "Tripwire backend up: network=testnet exchange=%s positions=%s trading=%s telegram=%s",
            adapter.name,
            {
                "account": "own account",
                "watch": f"WATCH {state.account_short} (read-only)",
                "price": "none (reference price only)",
            }[state.mode],
            adapter.status if adapter.can_trade else f"off ({adapter.detail})",
            bot.status,
        )
        try:
            yield
        finally:
            tasks = (warm_task, alert_task, voice_task, session_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await bot.stop()
            if feed_task:
                feed_task.cancel()
                await asyncio.gather(feed_task, return_exceptions=True)

    app = FastAPI(title="Tripwire backend", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET"],
        # the plugin sends this on every fetch to get past ngrok's free-tier browser warning
        allow_headers=["ngrok-skip-browser-warning", "content-type"],
    )
    app.include_router(
        make_ws_router(
            state=state,
            pairing_code=settings.pairing_code.get_secret_value(),
            store=store,
            adapter=adapter,
            hub=hub,
            guard=guard,
            orders=orders,
            on_snooze=on_snooze,
            inbox=inbox,
            replier=replier,
            send_reply=send_reply,
            voice=voice,
            status_line=lambda: status_line(state, adapter, guard),
            audio_override=audio_override,
            sessions=sessions,
            post_journal=post_journal,
            latency=latency,
            glyphs=settings.glyphs,
            active_alert=alerts.active_alert,
            on_order_result=on_order_result,
            min_interval_s=min_interval_s,
            heartbeat_s=heartbeat_s,
        )
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        age = state.feed_age_s()
        return {
            "ok": True,
            "network": "testnet",
            "hyperliquid": {
                "connected": state.connected,
                "last_msg_age_ms": None if age is None else int(age * 1000),
                "account_configured": bool(settings.hl_account_address),
                "mode": state.mode,
                "address": state.account_short,
                "positions": len(state.positions),
                "reconnects": feed.reconnects if feed else 0,
            },
            "trading": {
                "exchange": adapter.name,
                "enabled": adapter.can_trade,
                "status": adapter.status,
                "detail": adapter.detail,
                "dry_run": adapter.dry_run,
                "mode": "two-tap confirm; every order through the policy guard",
            },
            "policy": guard.status(),
            "voice": {"status": voice.status, "detail": voice.detail},
            "session": sessions.snapshot().to_ui(),
            "latency": latency.summary(),
            "alerts": {
                **alerts.status(asyncio.get_running_loop().time()),
                "away_mode": hub.count == 0,  # no glasses connected: alerts go to Telegram
            },
            "telegram": {"status": bot.status, "detail": bot.detail},
            "ws_clients": hub.count,
        }

    app.state.market = state  # handy for tests / debugging
    app.state.store = store
    app.state.adapter = adapter
    app.state.hub = hub
    app.state.guard = guard
    app.state.alerts = alerts
    app.state.on_order_result = on_order_result  # Away mode: what ends an alert episode
    app.state.bot_services = bot_services
    app.state.inbox = inbox
    app.state.voice = voice
    app.state.sessions = sessions
    app.state.bot = bot
    app.state.latency = latency
    app.state.replier = replier
    app.state.kill_switch = kill_switch
    return app
