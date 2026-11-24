"""Exchange-agnostic adapter interface.

Everything order-related in Tripwire talks to an `ExchangeAdapter`, never to an exchange SDK
directly, and only `OrderService.submit()` (orders.py) calls the order methods, after the policy
guard. Implementations:

  hyperliquid.HyperliquidAdapter   Hyperliquid TESTNET (live, or DRY_RUN: signed, not sent)
  fake.FakeAdapter                 in-memory exchange for tests and the simulator demo
  bybit_demo.BybitDemoAdapter      stub for Bybit's demo-trading API (shape only, no orders)

Interface (the four the brief asked for, plus helpers the screens need):
  stream_positions()      async iterator: the current positions, again after every change
  close_reduce_only(o)    reduce-only IOC close of part / all of a position
  set_sl(o)               place a new reduce-only stop-loss for the position, cancel the old one
  place_order(o)          open / add (limit or market) with its SL (and TP) attached
  current_stop(coin)      the position's current stop-loss (for "SL tighter 1%" / display)
  realized_pnl_today()    realized PnL since 00:00 UTC (policy guard's daily loss lock)
  realized_pnl_since(ms)  realized PnL since a time (session summary); WATCH: the watched account
  size_decimals(coin)     lot size, so the confirm screen shows exactly what will be sent
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from tripwire.hl.positions import Position
from tripwire.orders import CloseOrder, OpenOrder, OrderResult, StopOrder
from tripwire.state import MarketState

TradingStatus = Literal["off", "warming", "ready", "error"]


@dataclass(frozen=True, slots=True)
class StopInfo:
    """An existing stop-loss trigger order protecting a position."""

    coin: str
    oid: int
    trigger_px: float


@runtime_checkable
class ExchangeAdapter(Protocol):
    name: str  # "hyperliquid" | "fake" | "bybit_demo"
    status: TradingStatus
    detail: str
    can_trade: bool  # account + signer configured and not WATCH mode
    dry_run: bool  # orders are built and signed but NOT sent

    async def start(self) -> None: ...

    def stream_positions(self) -> AsyncIterator[list[Position]]: ...

    def size_decimals(self, coin: str) -> int | None: ...

    async def close_reduce_only(self, order: CloseOrder) -> OrderResult: ...

    async def set_sl(self, order: StopOrder) -> OrderResult: ...

    async def place_order(self, order: OpenOrder) -> OrderResult: ...

    async def current_stop(self, coin: str) -> StopInfo | None: ...

    async def realized_pnl_today(self) -> float: ...

    async def realized_pnl_since(self, start_ms: int) -> float: ...


async def positions_stream(state: MarketState) -> AsyncIterator[list[Position]]:
    """Yield the current positions now and after every MarketState change (bursts coalesce).
    Adapters that keep their positions in MarketState (Hyperliquid's feed, the fake) use this."""
    changed = asyncio.Event()
    state.add_listener(changed.set)
    try:
        yield list(state.positions)
        while True:
            await changed.wait()
            changed.clear()
            yield list(state.positions)
    finally:
        state.remove_listener(changed.set)
