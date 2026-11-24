"""Order types, and `OrderService`: THE single path by which any order leaves this process.

    glasses / Telegram -> ActionFlow (two-tap confirm) -> OrderService.submit()
                                                            1. PolicyGuard.check()   (policy.py)
                                                            2. exchange adapter call  (exchanges/)

Nothing else in the codebase calls an exchange adapter's order methods; `submit()` re-runs the
policy check right before sending (the flow also pre-checks at arm time so the user sees a block
reason before confirming, but state can change in between).

Order kinds:
  CloseOrder  reduce-only IOC close of part / all of a position       (always reduces risk)
  StopOrder   move a position's stop-loss (cancel old SL, place new)   (allowed only if tighter)
  OpenOrder   open / add to a position (forwarded drafts, item 6)      (needs SL, size limits)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tripwire.hl.positions import Position, Side

if TYPE_CHECKING:
    from tripwire.policy import PolicyGuard

log = logging.getLogger(__name__)

ResultStatus = Literal["filled", "resting", "placed", "rejected", "error", "blocked", "dry_run"]


@dataclass(frozen=True, slots=True)
class CloseOrder:
    coin: str
    side: Side  # side of the position being reduced (what the user saw and confirmed)
    fraction: float  # 0.25 / 0.5 / 1.0
    size: float | None  # coin units to close; None = the whole live position
    kind: Literal["close"] = "close"


@dataclass(frozen=True, slots=True)
class StopOrder:
    """Replace a position's stop-loss with a new reduce-only positionTpsl SL at `trigger_px`."""

    coin: str
    side: Side  # side of the position being protected
    trigger_px: float
    old_trigger_px: float | None  # current SL trigger (None = the position has no SL yet)
    old_oid: int | None  # exchange order id of the current SL (cancelled after the new one lands)
    label: str  # "SL to breakeven" / "SL tighter 1%"
    kind: Literal["stop"] = "stop"


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """Open or add to a position. Always carries a stop-loss (the policy guard insists)."""

    coin: str
    side: Side  # direction of the new exposure
    notional_usd: float
    leverage: float
    entry_px: float | None  # limit price; None = market (IOC at mid ± slippage)
    sl_px: float | None
    tp_px: float | None
    kind: Literal["open"] = "open"


Order = CloseOrder | StopOrder | OpenOrder


@dataclass(frozen=True, slots=True)
class OrderResult:
    ok: bool
    status: ResultStatus
    message: str  # short, human, shown on the glasses
    filled_size: float | None = None
    avg_px: float | None = None
    oid: int | None = None


def reduces_risk(order: Order) -> bool:
    """Closes and stop-loss moves only ever shrink exposure (the guard checks SLs are tighter)."""
    return order.kind in ("close", "stop")


class OrderService:
    """Policy guard first, then the exchange. `send` is the adapter call for this order."""

    def __init__(self, guard: PolicyGuard) -> None:
        self.guard = guard

    async def submit(
        self,
        order: Order,
        *,
        position: Position | None,
        mark: float | None,
        send: Callable[[Order], Awaitable[OrderResult]],
    ) -> OrderResult:
        await self.guard.refresh()  # fresh realized PnL for the daily loss lock
        decision = self.guard.check(order, position=position, mark=mark)
        if not decision.allowed:
            log.warning("ORDER BLOCKED by policy (%s): %s", decision.rule, decision.reason)
            return OrderResult(False, "blocked", decision.reason)
        log.info("policy ok (%s) for %s %s", decision.rule, order.kind, order.coin)
        return await send(order)
