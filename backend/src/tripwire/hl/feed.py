"""Live Hyperliquid TESTNET feed over WebSocket -> MarketState.

Why not the SDK's ``Info.subscribe``? hyperliquid-python-sdk 0.24.0's WebsocketManager only routes
a fixed list of channels (no ``clearinghouseState``), runs on a thread, and never reconnects.
``webData2`` (which it does route) has been removed from the API. So this is a small asyncio-native
client with the same wire protocol, plus reconnect + resubscribe + staleness watchdog.
The SDK is still the source of truth for the testnet URL (see config.assert_testnet).

Subscriptions:
  allMids                       -> mids for every coin (testnet pushes ~every 5 s)
  activeAssetCtx {coin}         -> markPx + midPx, ~1 Hz, for the reference coin + position coins
  clearinghouseState {user}     -> positions (only when HL_ACCOUNT_ADDRESS is set)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from tripwire.config import assert_testnet
from tripwire.state import MarketState

log = logging.getLogger(__name__)

PING_EVERY_S = 30.0  # HL closes connections that send nothing for 60 s
STALE_AFTER_S = 30.0  # no message at all for this long -> reconnect
BACKOFF_MAX_S = 30.0
MAX_MSG_BYTES = 8 * 1024 * 1024  # allMids lists ~3.5k coins


class StaleFeedError(RuntimeError):
    pass


def ws_url_for(api_url: str) -> str:
    assert api_url.startswith("https://")
    return "wss://" + api_url[len("https://") :] + "/ws"


def apply_ws_message(state: MarketState, msg: Mapping[str, Any]) -> None:
    """Route one decoded Hyperliquid WS message into the state. Pure apart from `state`."""
    channel = msg.get("channel")
    data = msg.get("data")
    if channel == "allMids" and isinstance(data, Mapping):
        mids = data.get("mids")
        if isinstance(mids, Mapping):
            state.update_mids(mids)
    elif channel == "activeAssetCtx" and isinstance(data, Mapping):
        coin, ctx = data.get("coin"), data.get("ctx")
        if isinstance(coin, str) and isinstance(ctx, Mapping):
            state.update_asset_ctx(coin, ctx)
    elif channel == "clearinghouseState" and isinstance(data, Mapping):
        user = str(data.get("user", "")).lower()
        ch = data.get("clearinghouseState")
        if user == state.account and isinstance(ch, Mapping):
            state.update_clearinghouse(ch)
    elif channel == "error":
        log.warning("Hyperliquid WS error: %s", data)
    # "subscriptionResponse" and "pong" need no handling


class HyperliquidFeed:
    def __init__(
        self,
        api_url: str,
        state: MarketState,
        *,
        stale_after_s: float = STALE_AFTER_S,
        ping_every_s: float = PING_EVERY_S,
    ) -> None:
        assert_testnet(api_url)  # second line of defence; create_app() already checked
        self.ws_url = ws_url_for(api_url)
        self.state = state
        self.stale_after_s = stale_after_s
        self.ping_every_s = ping_every_s
        self._ctx_coins: set[str] = set()
        self.reconnects = 0

    async def run(self) -> None:
        """Run forever: connect, subscribe, pump; on any failure back off and reconnect."""
        backoff = 1.0
        while True:
            try:
                async with connect(
                    self.ws_url, ping_interval=None, open_timeout=15, max_size=MAX_MSG_BYTES
                ) as ws:
                    log.info("Hyperliquid WS connected: %s", self.ws_url)
                    backoff = 1.0
                    self._ctx_coins = set()
                    self.state.touch()
                    await self._subscribe_initial(ws)
                    self.state.set_connected(True)
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, stale feed, server close...
                log.warning("Hyperliquid WS dropped (%s: %s)", type(exc).__name__, exc)
            finally:
                self.state.set_connected(False)
            self.reconnects += 1
            log.info("Reconnecting to Hyperliquid in %.0f s", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _send(self, ws: ClientConnection, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload))

    async def _subscribe(self, ws: ClientConnection, subscription: dict[str, Any]) -> None:
        await self._send(ws, {"method": "subscribe", "subscription": subscription})

    async def _subscribe_initial(self, ws: ClientConnection) -> None:
        await self._subscribe(ws, {"type": "allMids"})
        if self.state.account:
            await self._subscribe(ws, {"type": "clearinghouseState", "user": self.state.account})
        await self._sync_ctx_subscriptions(ws)

    async def _sync_ctx_subscriptions(self, ws: ClientConnection) -> None:
        """Subscribe activeAssetCtx for any newly watched coin (ref coin + open positions).
        Coins that close stay subscribed until the next reconnect -- harmless and simpler."""
        for coin in sorted(self.state.watched_coins() - self._ctx_coins):
            await self._subscribe(ws, {"type": "activeAssetCtx", "coin": coin})
            self._ctx_coins.add(coin)

    async def _pump(self, ws: ClientConnection) -> None:
        last_ping = time.monotonic()
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except TimeoutError:
                raw = None
            now = time.monotonic()
            if raw is not None:
                self.state.touch(now)
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("Ignoring non-JSON WS frame: %.80r", raw)
                    msg = None
                if isinstance(msg, Mapping):
                    apply_ws_message(self.state, msg)
                    if msg.get("channel") == "clearinghouseState":
                        await self._sync_ctx_subscriptions(ws)
            age = self.state.feed_age_s(now)
            if age is not None and age > self.stale_after_s:
                raise StaleFeedError(f"no Hyperliquid message for {age:.0f} s")
            if now - last_ping >= self.ping_every_s:
                await self._send(ws, {"method": "ping"})
                last_ping = now
