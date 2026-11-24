"""Hyperliquid TESTNET adapter: positions from the WebSocket feed, orders signed with the agent key.

Positions stream: hl/feed.py (run by main.py) writes clearinghouseState into MarketState;
`stream_positions()` yields from there, so the HUD and the risk engine see the same data.

Orders are built from the SDK's own pieces (`order_request_to_order_wire`,
`order_wires_to_order_action`, `sign_l1_action`) instead of calling `Exchange.market_close()`, so
there is ONE build-and-sign path, and DRY_RUN runs exactly that path and only skips the final
POST. Every action is signed with `is_mainnet=False` and `assert_testnet()` runs before every
signature.

DRY_RUN (default on, `.env DRY_RUN=true`): the signer is the agent key if set, otherwise a
throwaway key generated at startup (never logged, never stored). Each signed payload is logged and
appended to `backend/data/dry_run_orders.jsonl`; nothing is POSTed. Coin metadata still comes from
the public /info endpoint, so asset ids, lot sizes and tick rounding are the real ones.

Order shapes (all reduce-only except the opening leg of place_order):
  close_reduce_only  IOC limit at mid ± 5 % for `size` (rounded down to szDecimals), reduce_only
  set_sl             trigger {isMarket: true, tpsl: "sl"} for the position size, reduce_only,
                     grouping "positionTpsl", limit px = trigger -+ 10 % (slippage room, as in the
                     SDK's examples/basic_tpsl.py); THEN cancel the old SL (placing first means the
                     position is never left without a stop if the new one is rejected)
  place_order        updateLeverage (cross), then entry (GTC limit at entry, or IOC at mid ± 5 %)
                     + SL trigger (+ TP trigger) as one action, grouping "normalTpsl"
Not yet verified against a live testnet account (no funded account on Day 2): the positionTpsl
size convention (we send the position size; the web UI is reported to send 0) -- see
docs/BUILD_LOG.md §15.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from tripwire.config import TESTNET_API_URL, TestnetGuardError, assert_testnet
from tripwire.exchanges.base import StopInfo, TradingStatus, positions_stream
from tripwire.hl.positions import Position, to_float
from tripwire.orders import CloseOrder, OpenOrder, OrderResult, StopOrder
from tripwire.state import MarketState
from tripwire.trading import (
    DRY_RUN_REPLY,
    HTTP_TIMEOUT_S,
    ORDER_TIMEOUT_S,
    SLIPPAGE,
    parse_order_response,
    round_px,
    round_size_down,
    short_message,
    trigger_limit_px,
)

log = logging.getLogger(__name__)

Poster = Callable[[dict[str, Any]], Any]  # blocking POST /exchange (runs in a thread)
InfoFactory = Callable[[], Any]  # builds the SDK Info (2 blocking HTTP calls)


def _wire(
    asset: int, is_buy: bool, sz: float, px: float, order_type: dict[str, Any], reduce_only: bool
) -> Any:
    from hyperliquid.utils.signing import order_request_to_order_wire

    request = {
        "coin": "",  # unused by the wire format; the asset index is passed separately
        "is_buy": is_buy,
        "sz": sz,
        "limit_px": px,
        "order_type": order_type,
        "reduce_only": reduce_only,
    }
    return order_request_to_order_wire(request, asset)


def _order_action(wires: list[Any], grouping: str) -> dict[str, Any]:
    from hyperliquid.utils.signing import order_wires_to_order_action

    action: dict[str, Any] = order_wires_to_order_action(wires, None, grouping)
    return action


def utc_day_start_ms(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC)
    start = now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def pick_stop(open_orders: list[Any], coin: str, side: str) -> StopInfo | None:
    """The tightest reduce-only stop trigger on `coin` (frontendOpenOrders rows)."""
    stops: list[StopInfo] = []
    for o in open_orders:
        if not isinstance(o, dict) or o.get("coin") != coin or not o.get("isTrigger"):
            continue
        if not o.get("reduceOnly") or "Stop" not in str(o.get("orderType", "")):
            continue
        trig, oid = to_float(o.get("triggerPx")), o.get("oid")
        if trig is not None and isinstance(oid, int):
            stops.append(StopInfo(coin, oid, trig))
    if not stops:
        return None
    # long: the highest stop is the one that fires first; short: the lowest
    return max(stops, key=lambda s: s.trigger_px if side == "LONG" else -s.trigger_px)


class HyperliquidAdapter:
    name = "hyperliquid"

    def __init__(
        self,
        state: MarketState,
        *,
        base_url: str,
        account_address: str,
        agent_key: str = "",
        watch: bool = False,
        dry_run: bool = False,
        dry_run_log: Path | None = None,
        info_factory: InfoFactory | None = None,
        poster: Poster | None = None,
    ) -> None:
        assert_testnet(base_url)  # never build a signer for anything but testnet
        self.state = state
        self.base_url = base_url
        self.account = account_address
        self.watch = watch
        self._wallet: Any = None
        self.agent_address = ""
        self.signer_kind = ""  # "agent" | "throwaway"
        self.dry_run = dry_run
        self.dry_run_log = dry_run_log
        self.dry_run_records: deque[dict[str, Any]] = deque(maxlen=50)  # golive:check reads these
        if agent_key or dry_run:
            import eth_account

            if agent_key:
                self._wallet = eth_account.Account.from_key(agent_key)  # the key is not kept
                self.signer_kind = "agent"
            else:  # DRY_RUN without a key: a fresh throwaway key, only to sign real payloads
                self._wallet = eth_account.Account.create()
                self.signer_kind = "throwaway"
            self.agent_address = str(self._wallet.address)
        # dry run may sign without an account (forwarded drafts in price-only mode); live may not
        has_account = bool(account_address) or dry_run
        self.can_trade = bool(has_account and self._wallet is not None and not watch)
        self.status: TradingStatus = "warming" if self.can_trade else "off"
        self.detail = "loading coin metadata" if self.can_trade else self._off_reason()
        self._info: Any = None
        self._info_factory = info_factory or self._default_info
        self._poster: Poster = poster or self._default_post
        self._lock = asyncio.Lock()  # one order at a time

    # --- setup -------------------------------------------------------------------------------
    def _off_reason(self) -> str:
        if self.watch:
            return "watch only: WATCH_ADDRESS is someone else's account"
        return "set HL_ACCOUNT_ADDRESS and HL_AGENT_KEY in .env"

    def _default_info(self) -> Any:
        from hyperliquid.info import Info

        return Info(self.base_url, True, timeout=HTTP_TIMEOUT_S)  # skip_ws=True

    def _default_post(self, payload: dict[str, Any]) -> Any:
        from hyperliquid.api import API

        return API(self.base_url, HTTP_TIMEOUT_S).post("/exchange", payload)

    @property
    def signer_short(self) -> str:
        a = self.agent_address
        return f"{a[:6]}...{a[-4:]}" if a else ""

    async def _get_info(self) -> Any:
        if self._info is None:
            info = await asyncio.to_thread(self._info_factory)
            if getattr(info, "base_url", TESTNET_API_URL) != TESTNET_API_URL:
                raise TestnetGuardError("Info is not pointed at the testnet URL")
            self._info = info
            if self.can_trade:
                signer = f"{self.signer_kind} key {self.signer_short}"
                if self.dry_run:
                    self.status, self.detail = "ready", f"DRY RUN: signed with {signer}, NOT sent"
                    log.info("DRY RUN on: orders signed with %s and logged, never sent", signer)
                else:
                    self.status, self.detail = "ready", f"LIVE on testnet: {signer}"
                    log.info("Trading LIVE on testnet: %s", signer)
        return self._info

    async def start(self) -> None:
        """Warm-up: load coin metadata so the confirm screen shows exact rounded sizes."""
        if not self.can_trade:
            return
        try:
            await self._get_info()
        except Exception as exc:
            self.status, self.detail = "error", short_message(f"{type(exc).__name__}: {exc}")
            log.warning("Trading warm-up failed (will retry on first order): %s", self.detail)

    def stream_positions(self) -> AsyncIterator[list[Position]]:
        return positions_stream(self.state)

    # --- lookups -----------------------------------------------------------------------------
    def size_decimals(self, coin: str) -> int | None:
        if self._info is None:
            return None
        try:
            return int(self._info.asset_to_sz_decimals[self._info.name_to_asset(coin)])
        except Exception:
            return None

    def _position(self, coin: str) -> Position | None:
        return next((p for p in self.state.positions if p.coin == coin), None)

    async def current_stop(self, coin: str) -> StopInfo | None:
        p = self._position(coin)
        if p is None or not self.account:
            return None
        info = await self._get_info()
        rows = await asyncio.to_thread(info.frontend_open_orders, self.account)
        return pick_stop(rows if isinstance(rows, list) else [], coin, p.side)

    async def realized_pnl_today(self) -> float:
        if not self.account or self.watch:
            return 0.0
        info = await self._get_info()
        fills = await asyncio.to_thread(info.user_fills_by_time, self.account, utc_day_start_ms())
        total = 0.0
        for f in fills if isinstance(fills, list) else []:
            total += (to_float(f.get("closedPnl")) or 0.0) - (to_float(f.get("fee")) or 0.0)
        return total

    # --- signing + sending (the one path) ------------------------------------------------------
    async def _submit(self, action: dict[str, Any], what: str) -> Any:
        from hyperliquid.utils.signing import get_timestamp_ms, sign_l1_action

        assert_testnet(self.base_url)
        nonce = get_timestamp_ms()
        signature = sign_l1_action(self._wallet, action, None, nonce, None, False)  # False=testnet
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }
        if self.dry_run:
            await asyncio.to_thread(self._record_dry_run, what, payload)
            return DRY_RUN_REPLY
        log.info("ORDER %s (testnet) action=%s", what, json.dumps(action, separators=(",", ":")))
        return await asyncio.wait_for(asyncio.to_thread(self._poster, payload), ORDER_TIMEOUT_S)

    def _record_dry_run(self, what: str, payload: dict[str, Any]) -> None:
        """The signed payload, exactly as it would have been POSTed to /exchange."""
        line = json.dumps(
            {"what": what, "signer": self.agent_address, "network": "testnet", "payload": payload},
            separators=(",", ":"),
        )
        log.info("DRY RUN (signed, NOT sent) %s: %s", what, line)
        self.dry_run_records.append(
            {"what": what, "signer": self.agent_address, "payload": payload}
        )
        if self.dry_run_log is not None:
            self.dry_run_log.parent.mkdir(parents=True, exist_ok=True)
            with self.dry_run_log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    async def _guarded(self, what: str, run: Callable[[Any], Any]) -> OrderResult:
        """Lock, metadata, and error mapping shared by every order method."""
        if self._wallet is None or not self.can_trade:
            return OrderResult(False, "error", f"Trading off: {self._off_reason()}")
        async with self._lock:
            try:
                info = await self._get_info()
            except TestnetGuardError as exc:
                log.critical("ORDER refused: %s", exc)
                return OrderResult(False, "error", "Refused: exchange is not testnet")
            except Exception as exc:
                self.status, self.detail = "error", short_message(f"{type(exc).__name__}: {exc}")
                return OrderResult(False, "error", short_message(f"Hyperliquid unreachable: {exc}"))
            try:
                result: OrderResult = await run(info)
            except TimeoutError:  # the thread may still deliver it: never claim it failed
                return OrderResult(False, "error", "No reply in 25s - CHECK positions")
            except Exception as exc:
                message = getattr(exc, "error_message", None) or f"{type(exc).__name__}: {exc}"
                return OrderResult(False, "error", short_message(str(message)))
            log.info("ORDER %s -> %s: %s", what, result.status, result.message)
            return result

    # --- the four order operations -------------------------------------------------------------
    async def close_reduce_only(self, order: CloseOrder) -> OrderResult:
        async def run(info: Any) -> OrderResult:
            p = self._position(order.coin)
            if p is None or p.side != order.side:
                return OrderResult(False, "rejected", f"No open {order.side} {order.coin} position")
            decimals = self.size_decimals(order.coin)
            if decimals is None:
                return OrderResult(False, "error", f"Unknown coin {order.coin}")
            size = p.size if order.size is None else min(order.size, p.size)
            sz = round_size_down(size, decimals)
            if sz <= 0:
                return OrderResult(False, "rejected", "Size rounds to 0 - use a bigger %")
            mid = self.state.mids.get(order.coin) or p.mid
            is_buy = p.side == "SHORT"  # closing a short buys
            px = round_px(mid * (1 + SLIPPAGE) if is_buy else mid * (1 - SLIPPAGE), decimals)
            wire = _wire(
                info.name_to_asset(order.coin), is_buy, sz, px, {"limit": {"tif": "Ioc"}}, True
            )
            resp = await self._submit(_order_action([wire], "na"), f"close {order.coin} {sz}")
            result = parse_order_response(resp)
            if result.status == "dry_run":
                verb = "buy" if is_buy else "sell"
                msg = f"DRY RUN: {verb} {sz:g} {order.coin} IOC @ {px:g} signed, not sent"
                return OrderResult(True, "dry_run", msg)
            return result

        return await self._guarded(f"close {order.coin}", run)

    async def set_sl(self, order: StopOrder) -> OrderResult:
        async def run(info: Any) -> OrderResult:
            p = self._position(order.coin)
            if p is None or p.side != order.side:
                return OrderResult(False, "rejected", f"No open {order.side} {order.coin} position")
            decimals = self.size_decimals(order.coin)
            if decimals is None:
                return OrderResult(False, "error", f"Unknown coin {order.coin}")
            asset = info.name_to_asset(order.coin)
            trig = round_px(order.trigger_px, decimals)
            sz = round_size_down(p.size, decimals)
            is_buy = p.side == "SHORT"  # a short's stop buys back
            stop_type = {"trigger": {"triggerPx": trig, "isMarket": True, "tpsl": "sl"}}
            limit = trigger_limit_px(trig, is_buy, decimals)  # slippage room, see trading.py
            wire = _wire(asset, is_buy, sz, limit, stop_type, True)
            placed = parse_order_response(
                await self._submit(_order_action([wire], "positionTpsl"), f"SL {order.coin} {trig}")
            )
            if not placed.ok:
                return placed  # old SL untouched: the position is still protected
            if placed.status == "dry_run":
                message = f"DRY RUN: SL {trig:g} signed"
                if order.old_oid is not None:
                    cancel = {"type": "cancel", "cancels": [{"a": asset, "o": order.old_oid}]}
                    await self._submit(cancel, f"cancel old SL {order.old_oid}")
                    message += " + cancel of old SL"
                return OrderResult(True, "dry_run", message + ", not sent")
            message = f"SL set at {trig:g}"
            if order.old_oid is not None:
                cancel = {"type": "cancel", "cancels": [{"a": asset, "o": order.old_oid}]}
                gone = parse_order_response(
                    await self._submit(cancel, f"cancel old SL {order.old_oid}")
                )
                message += (
                    "; old SL cancelled" if gone.ok else f"; old SL NOT cancelled: {gone.message}"
                )
            return OrderResult(True, "placed", short_message(message), oid=placed.oid)

        return await self._guarded(f"set SL {order.coin}", run)

    async def place_order(self, order: OpenOrder) -> OrderResult:
        async def run(info: Any) -> OrderResult:
            decimals = self.size_decimals(order.coin)
            if decimals is None:
                return OrderResult(False, "error", f"Unknown coin {order.coin}")
            asset = info.name_to_asset(order.coin)
            mid = self.state.mids.get(order.coin)
            ref = order.entry_px if order.entry_px is not None else mid
            if ref is None or order.sl_px is None:
                return OrderResult(False, "error", f"No price for {order.coin}")
            sz = round_size_down(order.notional_usd / ref, decimals)
            if sz <= 0:
                return OrderResult(False, "rejected", "Size rounds to 0 - raise the notional")
            leverage = {
                "type": "updateLeverage",
                "asset": asset,
                "isCross": True,
                "leverage": round(order.leverage),
            }
            lev = await self._submit(leverage, f"leverage {order.coin} {round(order.leverage)}x")
            if not (isinstance(lev, dict) and lev.get("status") == "ok"):
                return OrderResult(False, "rejected", short_message(f"Leverage not set: {lev}"))
            is_buy = order.side == "LONG"
            if order.entry_px is not None:
                gtc = {"limit": {"tif": "Gtc"}}
                entry = _wire(asset, is_buy, sz, round_px(order.entry_px, decimals), gtc, False)
            else:
                px = ref * (1 + SLIPPAGE) if is_buy else ref * (1 - SLIPPAGE)
                entry = _wire(
                    asset, is_buy, sz, round_px(px, decimals), {"limit": {"tif": "Ioc"}}, False
                )
            legs = [entry]
            for kind, trigger in (("sl", order.sl_px), ("tp", order.tp_px)):
                if trigger is None:
                    continue
                t = round_px(trigger, decimals)
                trigger_type = {"trigger": {"triggerPx": t, "isMarket": True, "tpsl": kind}}
                limit = trigger_limit_px(t, not is_buy, decimals)
                legs.append(_wire(asset, not is_buy, sz, limit, trigger_type, True))
            what = f"open {order.side} {order.coin} {sz}"
            return parse_order_response(await self._submit(_order_action(legs, "normalTpsl"), what))

        return await self._guarded(f"open {order.coin}", run)
