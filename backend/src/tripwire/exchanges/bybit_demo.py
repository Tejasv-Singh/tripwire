"""Bybit demo-trading adapter: a STUB that shows how a second exchange plugs in. No requests.

Bybit's demo trading (https://api-demo.bybit.com, v5 API) would map like this:
  stream_positions   WS private "position" topic (wss://stream-demo.bybit.com/v5/private),
                     or GET /v5/position/list?category=linear
  close_reduce_only  POST /v5/order/create {category:linear, side:<opposite>, orderType:Market,
                     qty, reduceOnly:true}
  set_sl             POST /v5/position/trading-stop {category:linear, symbol, stopLoss,
                     tpslMode:Full} (Bybit replaces the position SL in one call: no cancel step)
  place_order        POST /v5/position/set-leverage, then /v5/order/create with stopLoss/takeProfit
  current_stop       GET /v5/position/list -> stopLoss
  realized_pnl_today GET /v5/position/closed-pnl?startTime=<00:00 UTC>
  size_decimals      GET /v5/market/instruments-info -> lotSizeFilter.qtyStep
Auth: HMAC-SHA256 headers (X-BAPI-API-KEY / -SIGN / -TIMESTAMP) with a *demo* key.
The same safety rules would apply: demo URL guard, policy guard via OrderService, two-tap confirm.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from tripwire.exchanges.base import StopInfo, TradingStatus
from tripwire.hl.positions import Position
from tripwire.orders import CloseOrder, OpenOrder, OrderResult, StopOrder

BYBIT_DEMO_URL = "https://api-demo.bybit.com"
NOT_IMPLEMENTED = "Bybit demo adapter is a stub - orders are not implemented"


class BybitDemoAdapter:
    name = "bybit_demo"

    def __init__(self, base_url: str = BYBIT_DEMO_URL) -> None:
        if base_url != BYBIT_DEMO_URL:  # same idea as assert_testnet: demo only, exact match
            raise ValueError(f"Refusing {base_url!r}: the Bybit adapter is demo-only")
        self.base_url = base_url
        self.status: TradingStatus = "off"
        self.detail = NOT_IMPLEMENTED
        self.can_trade = False
        self.dry_run = False

    async def start(self) -> None:
        return None

    async def _no_positions(self) -> AsyncIterator[list[Position]]:
        yield []

    def stream_positions(self) -> AsyncIterator[list[Position]]:
        return self._no_positions()

    def size_decimals(self, coin: str) -> int | None:
        return None

    async def close_reduce_only(self, order: CloseOrder) -> OrderResult:
        return OrderResult(False, "error", NOT_IMPLEMENTED)

    async def set_sl(self, order: StopOrder) -> OrderResult:
        return OrderResult(False, "error", NOT_IMPLEMENTED)

    async def place_order(self, order: OpenOrder) -> OrderResult:
        return OrderResult(False, "error", NOT_IMPLEMENTED)

    async def current_stop(self, coin: str) -> StopInfo | None:
        return None

    async def realized_pnl_today(self) -> float:
        return 0.0

    async def realized_pnl_since(self, start_ms: int) -> float:
        return 0.0
