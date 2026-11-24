"""Policy guard: the rules every order must pass before it can reach an exchange.

`check_order()` is pure (unit-tested rule by rule in tests/test_policy.py). `PolicyGuard` wraps it
with the live inputs: limits from .env, the kill switch, and realized PnL for today.
`OrderService.submit()` (orders.py) is the only caller that sends; the action flow also calls
`check()` at arm time so a block reason shows BEFORE the confirm screen.

Rules (first failing rule wins; `rule` names are stable and show up in logs/tests):
  every order   watch_only        WATCH mode never trades
  CloseOrder    reduce_only       allowed if the position exists on the confirmed side, ALWAYS --
                                  even with the kill switch on or the daily loss lock hit
  StopOrder     sl_tighten/sl_new allowed if the SL is on the protective side of mark and is
                                  tighter than the current one (or there was none); never blocked
                                  by the kill switch / loss lock (it only reduces risk)
                sl_loosen         moving a stop away from the price is refused
                sl_would_trigger  a "stop" past the mark would fire at once: use Close instead
  OpenOrder     kill_switch       KILL_SWITCH=true in .env or /kill in Telegram: no new exposure
                daily_loss_lock   realized PnL today (UTC) <= -DAILY_LOSS_LIMIT_USD
                sl_required       every opening order needs a stop-loss
                max_notional      notional > MAX_OPEN_NOTIONAL_USD ($250)
                max_leverage      leverage > MAX_LEVERAGE (5x)
                sl_wrong_side / tp_wrong_side / opposite_position / bad_size / no_price
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tripwire.db import Store
from tripwire.hl.positions import Position
from tripwire.orders import CloseOrder, OpenOrder, Order, StopOrder

log = logging.getLogger(__name__)

KILL_SWITCH_KEY = "kill_switch"


@dataclass(frozen=True, slots=True)
class PolicyLimits:
    max_open_notional_usd: float = 250.0
    max_leverage: float = 5.0
    daily_loss_limit_usd: float = 100.0


@dataclass(frozen=True, slots=True)
class PolicyContext:
    position: Position | None  # the live position for order.coin (None = flat)
    mark: float | None  # mark price for order.coin
    kill_switch: bool = False
    realized_pnl_today: float = 0.0  # USD, UTC day; negative = loss
    watch_only: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    rule: str
    reason: str  # short, human, shown on the glasses / in Telegram


def _ok(rule: str, reason: str = "ok") -> Decision:
    return Decision(True, rule, reason)


def _no(rule: str, reason: str) -> Decision:
    return Decision(False, rule, reason)


def _usd(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"


def _px(x: float) -> str:
    return f"{x:,.6g}" if x < 1000 else f"{x:,.0f}"


def check_order(order: Order, ctx: PolicyContext, limits: PolicyLimits) -> Decision:
    if ctx.watch_only:
        return _no("watch_only", "Watch only - this is not your account")
    if isinstance(order, CloseOrder):
        return _check_close(order, ctx)
    if isinstance(order, StopOrder):
        return _check_stop(order, ctx)
    return _check_open(order, ctx, limits)


def _check_close(order: CloseOrder, ctx: PolicyContext) -> Decision:
    p = ctx.position
    if p is None:
        return _no("no_position", f"No open {order.coin} position")
    if p.side != order.side:
        return _no("side_mismatch", f"{order.coin} position is now {p.side} - not sent")
    if not 0 < order.fraction <= 1:
        return _no("bad_size", "Close fraction must be between 0 and 100%")
    if order.size is not None and order.size <= 0:
        return _no("bad_size", "Close size must be positive")
    return _ok("reduce_only")


def _check_stop(order: StopOrder, ctx: PolicyContext) -> Decision:
    p = ctx.position
    if p is None:
        return _no("no_position", f"No open {order.coin} position")
    if p.side != order.side:
        return _no("side_mismatch", f"{order.coin} position is now {p.side} - not sent")
    if order.trigger_px <= 0:
        return _no("bad_price", "Stop price must be positive")
    if ctx.mark is None or ctx.mark <= 0:
        return _no("no_price", f"No mark price for {order.coin}")
    long = order.side == "LONG"
    if (long and order.trigger_px >= ctx.mark) or (not long and order.trigger_px <= ctx.mark):
        where = "above" if long else "below"
        return _no(
            "sl_would_trigger",
            f"SL {_px(order.trigger_px)} is {where} mark {_px(ctx.mark)} - it would fire at once",
        )
    old = order.old_trigger_px
    if old is None:
        return _ok("sl_new", "adds a stop-loss")
    tighter = order.trigger_px > old if long else order.trigger_px < old
    if not tighter:
        return _no(
            "sl_loosen",
            f"SL {_px(order.trigger_px)} is not tighter than {_px(old)} - loosening is refused",
        )
    return _ok("sl_tighten", "tightens the stop-loss")


def _check_open(order: OpenOrder, ctx: PolicyContext, limits: PolicyLimits) -> Decision:
    if ctx.kill_switch:
        return _no("kill_switch", "Kill switch ON - only reduce-only orders")
    if ctx.realized_pnl_today <= -limits.daily_loss_limit_usd:
        return _no(
            "daily_loss_lock",
            f"Daily loss lock: {_usd(ctx.realized_pnl_today)} today "
            f"(limit -{_usd(limits.daily_loss_limit_usd)})",
        )
    if order.sl_px is None:
        return _no("sl_required", "Opening orders need a stop-loss (sl)")
    if order.notional_usd <= 0 or order.leverage < 1:
        return _no("bad_size", "Notional must be > 0 and leverage >= 1x")
    if order.notional_usd > limits.max_open_notional_usd + 1e-9:
        return _no(
            "max_notional",
            f"Notional {_usd(order.notional_usd)} > max {_usd(limits.max_open_notional_usd)}",
        )
    if order.leverage > limits.max_leverage + 1e-9:
        return _no("max_leverage", f"Leverage {order.leverage:g}x > max {limits.max_leverage:g}x")
    px = order.entry_px if order.entry_px is not None else ctx.mark
    if px is None or px <= 0:
        return _no("no_price", f"No price for {order.coin}")
    long = order.side == "LONG"
    if (long and order.sl_px >= px) or (not long and order.sl_px <= px):
        rel = "below" if long else "above"
        side = "long" if long else "short"
        return _no(
            "sl_wrong_side", f"SL {_px(order.sl_px)} must be {rel} entry {_px(px)} for a {side}"
        )
    if order.tp_px is not None and (
        (long and order.tp_px <= px) or (not long and order.tp_px >= px)
    ):
        rel = "above" if long else "below"
        side = "long" if long else "short"
        return _no(
            "tp_wrong_side", f"TP {_px(order.tp_px)} must be {rel} entry {_px(px)} for a {side}"
        )
    p = ctx.position
    if p is not None and p.side != order.side:
        return _no(
            "opposite_position",
            f"You are {p.side} {order.coin} - a {order.side} would flip it; use Close",
        )
    return _ok("open_within_limits", "within limits, has a stop-loss")


# --- live wrapper --------------------------------------------------------------------------------


class KillSwitch:
    """ON if KILL_SWITCH=true in .env OR engaged via Telegram /kill (persisted in SQLite, so it
    survives restarts). /unkill can only release the Telegram flag, never the .env flag."""

    def __init__(self, env_flag: bool, store: Store | None) -> None:
        self.env_flag = env_flag
        self.store = store
        self._db_flag = False

    def load(self) -> None:
        if self.store is not None:
            self._db_flag = self.store.kv_get(KILL_SWITCH_KEY) == "on"

    @property
    def engaged(self) -> bool:
        return self.env_flag or self._db_flag

    @property
    def source(self) -> str:
        if self.env_flag:
            return "env"
        return "telegram" if self._db_flag else "off"

    def engage(self) -> None:
        self._db_flag = True
        if self.store is not None:
            self.store.kv_set(KILL_SWITCH_KEY, "on")
        log.warning("KILL SWITCH engaged (Telegram): only reduce-only orders from now on")

    def release(self) -> bool:
        """False if the .env flag keeps it on."""
        self._db_flag = False
        if self.store is not None:
            self.store.kv_set(KILL_SWITCH_KEY, "off")
        log.warning("Kill switch released via Telegram (env flag=%s)", self.env_flag)
        return not self.env_flag


PnlSource = Callable[[], Awaitable[float]]


async def _no_pnl() -> float:
    return 0.0


class PolicyGuard:
    def __init__(
        self,
        limits: PolicyLimits,
        kill_switch: KillSwitch,
        *,
        pnl_source: PnlSource = _no_pnl,
        watch_only: bool = False,
    ) -> None:
        self.limits = limits
        self.kill_switch = kill_switch
        self.pnl_source = pnl_source
        self.watch_only = watch_only
        self.realized_pnl_today = 0.0

    async def refresh(self) -> None:
        try:
            self.realized_pnl_today = await self.pnl_source()
        except Exception as exc:  # stale PnL is better than no guard; keep the last value
            log.warning("could not refresh realized PnL (%s: %s)", type(exc).__name__, exc)

    @property
    def loss_locked(self) -> bool:
        return self.realized_pnl_today <= -self.limits.daily_loss_limit_usd

    def check(self, order: Order, *, position: Position | None, mark: float | None) -> Decision:
        ctx = PolicyContext(
            position=position,
            mark=mark,
            kill_switch=self.kill_switch.engaged,
            realized_pnl_today=self.realized_pnl_today,
            watch_only=self.watch_only,
        )
        return check_order(order, ctx, self.limits)

    def status(self) -> dict[str, object]:
        return {
            "kill_switch": self.kill_switch.engaged,
            "kill_switch_source": self.kill_switch.source,
            "realized_pnl_today": round(self.realized_pnl_today, 2),
            "loss_locked": self.loss_locked,
            "max_open_notional_usd": self.limits.max_open_notional_usd,
            "max_leverage": self.limits.max_leverage,
            "daily_loss_limit_usd": self.limits.daily_loss_limit_usd,
        }
