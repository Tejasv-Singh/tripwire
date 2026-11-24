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
from tripwire.exchanges.base import ExchangeAdapter
from tripwire.exchanges.hyperliquid import HyperliquidAdapter
from tripwire.hl.feed import HyperliquidFeed
from tripwire.orders import OrderService
from tripwire.policy import KillSwitch, PolicyGuard, PolicyLimits
from tripwire.state import MarketState
from tripwire.telegram_bot import BotServices, TelegramBot, build_application
from tripwire.ws import ClientHub, make_ws_router

log = logging.getLogger("tripwire")


_SECRET_PATTERNS = [
    (re.compile(r"(code=)[^&\s\"']+"), r"\1***"),  # /ws?code=<PAIRING_CODE> in access logs
    (re.compile(r"(bot)\d+:[A-Za-z0-9_-]+"), r"\1***"),  # Telegram token inside API URLs
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


def create_app(
    settings: Settings | None = None,
    *,
    run_feed: bool = True,
    run_bot: bool = True,
    min_interval_s: float = 0.5,
    heartbeat_s: float = 5.0,
    make_adapter: Callable[[MarketState], ExchangeAdapter] | None = None,
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(store.init)
        await asyncio.to_thread(kill_switch.load)
        if kill_switch.engaged:
            log.warning("Kill switch is ON (%s): only reduce-only orders", kill_switch.source)
        feed_task = asyncio.create_task(feed.run(), name="hl-feed") if feed else None
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
            warm_task.cancel()
            await asyncio.gather(warm_task, return_exceptions=True)
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
            "telegram": {"status": bot.status, "detail": bot.detail},
            "ws_clients": hub.count,
        }

    app.state.market = state  # handy for tests / debugging
    app.state.store = store
    app.state.adapter = adapter
    app.state.hub = hub
    app.state.guard = guard
    app.state.kill_switch = kill_switch
    return app
