"""Reduce-only order execution on Hyperliquid TESTNET, signed with the agent (API) wallet key.

Tripwire places exactly ONE kind of order: a reduce-only IOC "market close" of part or all of an
open position, through hyperliquid-python-sdk's `Exchange.market_close()`:
  * reduce_only=True is hard-coded inside the SDK call, so an order can shrink a position but
    never open or flip one;
  * IOC limit at mid ± SLIPPAGE (5 %, the SDK default), so it never rests on the book;
  * the side is taken from the LIVE position the SDK reads just before sending.

Safety layers, outermost first:
  1. `ActionFlow` (actions.py) only produces a CloseRequest after the two-tap confirm.
  2. `HyperliquidExecutor` refuses to exist unless the URL is the testnet URL (`assert_testnet`)
     and re-checks the SDK object's `base_url` before the first order.
  3. One order at a time (`asyncio.Lock`).
The agent key never leaves this process: it is turned into an eth_account wallet at startup and
only the wallet's public address is ever logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal, Protocol

from tripwire.config import TESTNET_API_URL, TestnetGuardError, assert_testnet
from tripwire.hl.positions import Side, to_float

log = logging.getLogger(__name__)

SLIPPAGE = 0.05  # IOC limit = mid * (1 ± 5 %), the SDK's DEFAULT_SLIPPAGE
HTTP_TIMEOUT_S = 10.0  # per HTTP request made by the SDK (requests timeout)
ORDER_TIMEOUT_S = 25.0  # whole market_close() call: user_state + all_mids + order
MAX_MESSAGE_LEN = 120

TradingStatus = Literal["off", "warming", "ready", "error"]
ResultStatus = Literal["filled", "resting", "rejected", "error"]


@dataclass(frozen=True, slots=True)
class CloseRequest:
    coin: str
    side: Side  # side of the position being reduced (what the user saw and confirmed)
    fraction: float  # 0.25 / 0.5 / 1.0
    size: float | None  # coin units to close; None = the whole live position


@dataclass(frozen=True, slots=True)
class OrderResult:
    ok: bool
    status: ResultStatus
    message: str  # short, human, shown on the glasses
    filled_size: float | None = None
    avg_px: float | None = None
    oid: int | None = None


class OrderExecutor(Protocol):
    status: TradingStatus
    detail: str

    def size_decimals(self, coin: str) -> int | None: ...

    async def warm_up(self) -> None: ...

    async def close(self, req: CloseRequest) -> OrderResult: ...


class DisabledExecutor:
    """HL_ACCOUNT_ADDRESS or HL_AGENT_KEY is empty: the menu still works, orders never go out."""

    def __init__(self, detail: str = "set HL_ACCOUNT_ADDRESS and HL_AGENT_KEY in .env") -> None:
        self.status: TradingStatus = "off"
        self.detail = detail

    def size_decimals(self, coin: str) -> int | None:
        return None

    async def warm_up(self) -> None:
        return None

    async def close(self, req: CloseRequest) -> OrderResult:
        return OrderResult(False, "error", "Trading off: no agent key / account in .env")


# --- pure helpers (unit-tested) ------------------------------------------------------------------


def round_size_down(size: float, decimals: int) -> float:
    """Round DOWN to the coin's szDecimals so a partial close never exceeds what was shown."""
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(repr(size)).quantize(quantum, rounding=ROUND_DOWN))


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE_LEN else text[: MAX_MESSAGE_LEN - 3] + "..."


def parse_order_response(resp: Any) -> OrderResult:
    """Turn the SDK's /exchange reply into an OrderResult.

    ok:     {"status":"ok","response":{"type":"order","data":{"statuses":[{"filled":{...}}]}}}
            statuses[0] is {"filled":{"totalSz","avgPx","oid"}} | {"resting":{"oid"}} |
            {"error":"..."}
    not ok: {"status":"err","response":"<message>"}
    None:   market_close() found no position for that coin.
    """
    if resp is None:
        return OrderResult(False, "rejected", "No open position for that coin on the exchange")
    if not isinstance(resp, Mapping):
        return OrderResult(False, "error", _short(f"Unexpected reply: {resp!r}"))
    if resp.get("status") != "ok":
        detail = resp.get("response") or resp.get("error") or resp
        return OrderResult(False, "rejected", _short(str(detail)))
    response = resp.get("response")
    data = response.get("data") if isinstance(response, Mapping) else None
    statuses = data.get("statuses") if isinstance(data, Mapping) else None
    if not isinstance(statuses, list) or not statuses or not isinstance(statuses[0], Mapping):
        return OrderResult(False, "error", "Empty order reply from Hyperliquid")
    st = statuses[0]
    if isinstance(st.get("filled"), Mapping):
        f = st["filled"]
        sz, px = to_float(f.get("totalSz")), to_float(f.get("avgPx"))
        oid = f.get("oid") if isinstance(f.get("oid"), int) else None
        return OrderResult(
            True, "filled", f"Filled {f.get('totalSz')} @ {f.get('avgPx')}", sz, px, oid
        )
    if isinstance(st.get("resting"), Mapping):
        oid = st["resting"].get("oid")
        oid = oid if isinstance(oid, int) else None
        return OrderResult(True, "resting", f"Order resting (oid {oid})", oid=oid)
    if "error" in st:
        return OrderResult(False, "rejected", _short(str(st["error"])))
    return OrderResult(False, "error", _short(f"Unknown order status: {dict(st)!r}"))


# --- real executor -------------------------------------------------------------------------------

ExchangeFactory = Callable[[], Any]


class HyperliquidExecutor:
    def __init__(
        self,
        *,
        base_url: str,
        account_address: str,
        agent_key: str,
        exchange_factory: ExchangeFactory | None = None,
    ) -> None:
        assert_testnet(base_url)  # never even build a signer for anything but testnet
        self.base_url = base_url
        self.account_address = account_address
        self.status: TradingStatus = "warming"
        self.detail = "connecting to Hyperliquid"
        self._exchange: Any = None
        self._lock = asyncio.Lock()
        self.agent_address = "0x(test)"
        self._factory: ExchangeFactory
        if exchange_factory is not None:
            self._factory = exchange_factory
        else:
            import eth_account
            from hyperliquid.exchange import Exchange

            wallet = eth_account.Account.from_key(agent_key)
            self.agent_address = str(wallet.address)  # public; the key itself is not kept

            def build() -> Any:
                # Info(skip_ws=True) inside: two blocking HTTP calls (meta + spotMeta)
                return Exchange(
                    wallet, base_url, account_address=account_address, timeout=HTTP_TIMEOUT_S
                )

            self._factory = build

    async def _get_exchange(self) -> Any:
        if self._exchange is None:
            exchange = await asyncio.to_thread(self._factory)
            if getattr(exchange, "base_url", None) != TESTNET_API_URL:
                raise TestnetGuardError("Exchange is not pointed at the testnet URL")
            self._exchange = exchange
            short = f"{self.agent_address[:6]}...{self.agent_address[-4:]}"
            self.status, self.detail = "ready", f"agent {short}"
            log.info("Trading ready on testnet: agent wallet %s, reduce-only closes only", short)
        return self._exchange

    async def warm_up(self) -> None:
        """Load coin metadata in the background at startup so the confirm screen can show exact
        rounded sizes. Failure is not fatal: the first close() tries again."""
        try:
            await self._get_exchange()
        except Exception as exc:
            self.status, self.detail = "error", _short(f"{type(exc).__name__}: {exc}")
            log.warning("Trading warm-up failed (will retry on first order): %s", self.detail)

    def size_decimals(self, coin: str) -> int | None:
        if self._exchange is None:
            return None
        try:
            info = self._exchange.info
            return int(info.asset_to_sz_decimals[info.name_to_asset(coin)])
        except Exception:
            return None

    async def close(self, req: CloseRequest) -> OrderResult:
        async with self._lock:  # one order at a time
            try:
                exchange = await self._get_exchange()
            except TestnetGuardError as exc:
                log.critical("ORDER refused: %s", exc)
                return OrderResult(False, "error", "Refused: exchange is not testnet")
            except Exception as exc:
                self.status, self.detail = "error", _short(f"{type(exc).__name__}: {exc}")
                return OrderResult(False, "error", _short(f"Hyperliquid unreachable: {exc}"))

            sz: float | None = None
            if req.size is not None:
                decimals = self.size_decimals(req.coin)
                if decimals is None:
                    return OrderResult(False, "error", f"Unknown coin {req.coin}")
                sz = round_size_down(req.size, decimals)
                if sz <= 0:
                    return OrderResult(False, "rejected", "Size rounds to 0 - use a bigger %")

            log.info(
                "ORDER reduce-only close %s %s %.0f%% size=%s (testnet)",
                req.coin,
                req.side,
                req.fraction * 100,
                "ALL" if sz is None else sz,
            )
            try:
                resp = await asyncio.wait_for(
                    asyncio.to_thread(exchange.market_close, req.coin, sz, None, SLIPPAGE),
                    timeout=ORDER_TIMEOUT_S,
                )
            except TimeoutError:
                # the thread may still deliver the order: never claim it failed
                return OrderResult(False, "error", "No reply in 25s - CHECK positions")
            except Exception as exc:
                message = getattr(exc, "error_message", None) or f"{type(exc).__name__}: {exc}"
                return OrderResult(False, "error", _short(str(message)))
            result = parse_order_response(resp)
            log.info("ORDER result %s: %s", result.status, result.message)
            return result
