"""In-memory fake exchange: for tests and the simulator demo (`npm run backend:demo`).

It keeps Hyperliquid-shaped position rows, pushes them into MarketState (so the HUD, the risk
engine and the policy guard see them exactly like real ones), fills closes at the current mid
after `fill_delay_s`, keeps one stop-loss per coin, and tracks realized PnL. Nothing leaves the
process. The demo backend labels itself DEMO on the glasses.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator
from typing import Any

from tripwire.exchanges.base import StopInfo, TradingStatus, positions_stream
from tripwire.hl.positions import Position
from tripwire.orders import CloseOrder, OpenOrder, OrderResult, StopOrder
from tripwire.state import MarketState
from tripwire.trading import round_size_down


class FakeAdapter:
    name = "fake"

    def __init__(
        self,
        state: MarketState,
        positions: list[dict[str, Any]] | None = None,
        *,
        fill_delay_s: float = 0.0,
        sz_decimals: int = 4,
        stops: dict[str, float] | None = None,
    ) -> None:
        self.state = state
        self.rows: dict[str, dict[str, Any]] = {r["coin"]: dict(r) for r in positions or []}
        self.fill_delay_s = fill_delay_s
        self.decimals = sz_decimals
        self._oids = itertools.count(1000)
        self.stops: dict[str, StopInfo] = {
            coin: StopInfo(coin, next(self._oids), px) for coin, px in (stops or {}).items()
        }
        self.realized = 0.0
        self.realized_log: list[tuple[int, float]] = []  # (ms, pnl) per fill, for sessions
        self.calls: list[tuple[str, Any]] = []  # every order method call, for tests
        self.status: TradingStatus = "ready"
        self.detail = "FAKE exchange (in memory, nothing is sent)"
        self.can_trade = True
        self.dry_run = False
        self.publish()

    # --- state -------------------------------------------------------------------------------
    def publish(self) -> None:
        rows = [{"position": dict(r)} for r in self.rows.values() if float(r["szi"]) != 0]
        self.state.update_clearinghouse({"assetPositions": rows})

    def _mid(self, coin: str) -> float:
        p = next((p for p in self.state.positions if p.coin == coin), None)
        return self.state.mids.get(coin) or (p.mid if p else 0.0)

    async def start(self) -> None:
        return None

    def stream_positions(self) -> AsyncIterator[list[Position]]:
        return positions_stream(self.state)

    def size_decimals(self, coin: str) -> int | None:
        return self.decimals

    async def current_stop(self, coin: str) -> StopInfo | None:
        return self.stops.get(coin)

    async def realized_pnl_today(self) -> float:
        return self.realized

    async def realized_pnl_since(self, start_ms: int) -> float:
        return sum(pnl for ms, pnl in self.realized_log if ms >= start_ms)

    # --- orders ------------------------------------------------------------------------------
    async def close_reduce_only(self, order: CloseOrder) -> OrderResult:
        self.calls.append(("close", order))
        await asyncio.sleep(self.fill_delay_s)
        row = self.rows.get(order.coin)
        if row is None or float(row["szi"]) == 0:
            return OrderResult(False, "rejected", f"No open {order.coin} position")
        szi = float(row["szi"])
        size = abs(szi) if order.size is None else min(order.size, abs(szi))
        size = round_size_down(size, self.decimals)
        px = self._mid(order.coin)
        sign = 1 if szi > 0 else -1
        pnl = sign * size * (px - float(row["entryPx"]))
        self.realized += pnl
        self.realized_log.append((int(time.time() * 1000), pnl))
        row["szi"] = f"{szi - sign * size:.{self.decimals}f}"
        if float(row["szi"]) == 0:
            self.stops.pop(order.coin, None)  # a positionTpsl stop dies with its position
        self.publish()
        return OrderResult(
            True, "filled", f"Filled {size:g} @ {px:,.1f}", size, px, next(self._oids)
        )

    async def set_sl(self, order: StopOrder) -> OrderResult:
        self.calls.append(("set_sl", order))
        await asyncio.sleep(self.fill_delay_s)
        if order.coin not in self.rows or float(self.rows[order.coin]["szi"]) == 0:
            return OrderResult(False, "rejected", f"No open {order.coin} position")
        new = StopInfo(order.coin, next(self._oids), order.trigger_px)
        old = self.stops.get(order.coin)
        self.stops[order.coin] = new  # place first, then the old one is gone
        message = f"SL set at {order.trigger_px:g}"
        if old is not None:
            message += "; old SL cancelled"
        return OrderResult(True, "placed", message, oid=new.oid)

    async def place_order(self, order: OpenOrder) -> OrderResult:
        self.calls.append(("place", order))
        await asyncio.sleep(self.fill_delay_s)
        px = order.entry_px or self._mid(order.coin)
        if not px:
            return OrderResult(False, "error", f"No price for {order.coin}")
        size = round_size_down(order.notional_usd / px, self.decimals)
        if order.entry_px is not None:
            return OrderResult(True, "resting", f"Limit {order.side} {size:g} @ {px:,.6g} resting")
        sign = 1 if order.side == "LONG" else -1
        row = self.rows.setdefault(
            order.coin,
            {"coin": order.coin, "szi": "0", "entryPx": str(px), "liquidationPx": None,
             "leverage": {"type": "cross", "value": order.leverage}, "positionValue": "0"},
        )  # fmt: skip
        row["szi"] = f"{float(row['szi']) + sign * size:.{self.decimals}f}"
        if order.sl_px is not None:
            self.stops[order.coin] = StopInfo(order.coin, next(self._oids), order.sl_px)
        self.publish()
        return OrderResult(True, "filled", f"Filled {size:g} @ {px:,.6g}", size, px)
