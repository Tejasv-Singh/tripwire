"""Trading-session summary (screen 6, /summary, journal post).

A session starts when the backend starts and again right after one is ended (double tap on the
HUD -> "End session?" -> tap). What is counted:

  trades          every order result that changed something (filled / resting / placed);
                  dry-run signatures are counted separately ("+N dry run")
  realized P&L    sum(closedPnl - fee) of the account's fills since the session started
                  (adapter.realized_pnl_since; in WATCH mode the WATCHED account -- fills are
                  public), refreshed every REALIZED_REFRESH_S
  unrealized      sum of uPnL of the positions open now
  session P&L     realized + (unrealized now - unrealized when the session started): what changed
                  DURING the session, so a WATCH position opened last week doesn't count
  max drawdown    largest peak-to-trough fall of the session-P&L curve, sampled every SAMPLE_S
                  and on every result
  alerts          fired (risk.py delivered one), acted (an order sent from an alert screen),
                  snoozed
  policy blocks   orders the policy guard refused (pre-check, submit, forwarded drafts, WATCH)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from tripwire.db import Store
from tripwire.exchanges.base import ExchangeAdapter
from tripwire.orders import Order, OrderResult
from tripwire.state import MarketState

log = logging.getLogger(__name__)

SAMPLE_S = 5.0
REALIZED_REFRESH_S = 30.0
MAX_JOURNAL_TRADES = 15


@dataclass(frozen=True, slots=True)
class TradeRecord:
    at_ms: int
    kind: str  # close / stop / open
    coin: str
    side: str
    fraction: float | None
    status: str  # filled / resting / placed / dry_run
    message: str
    via_alert: bool = False


@dataclass(frozen=True, slots=True)
class SessionSummary:
    started_ms: int
    ended_ms: int | None  # None = still running (a live snapshot)
    mode: str  # "own account" / "WATCH 0x1234...abcd" / "price only" (+ DRY RUN / DEMO)
    trades: int
    dry_runs: int
    realized: float
    unrealized: float
    session_pnl: float
    max_drawdown: float  # <= 0
    alerts_fired: int
    alerts_acted: int
    alerts_snoozed: int
    policy_blocks: int
    trade_log: list[TradeRecord] = field(default_factory=list)

    @property
    def duration_s(self) -> int:
        end = self.ended_ms if self.ended_ms is not None else int(time.time() * 1000)
        return max(0, (end - self.started_ms) // 1000)

    def to_ui(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("trade_log")
        d["duration_s"] = self.duration_s
        return d

    def text(self, tz: dt.tzinfo | None = None) -> str:
        """The journal post / `/summary` reply."""

        def at(ms: int) -> str:
            return dt.datetime.fromtimestamp(ms / 1000, tz).strftime("%H:%M")

        h, m = divmod(self.duration_s // 60, 60)
        day = dt.datetime.fromtimestamp(self.started_ms / 1000, tz).strftime("%d %b %Y")
        span = f"{at(self.started_ms)}-{at(self.ended_ms) if self.ended_ms else 'now'}"
        trades = f"{self.trades}" + (f" (+{self.dry_runs} dry run)" if self.dry_runs else "")
        lines = [
            f"Tripwire session {day} {span} ({h}h{m:02d}m) - Hyperliquid TESTNET",
            f"Mode: {self.mode}",
            f"Trades: {trades}",
        ]
        for t in self.trade_log[-MAX_JOURNAL_TRADES:]:
            what = f"{t.kind} {int(t.fraction * 100)}%" if t.fraction else t.kind
            flag = " (from alert)" if t.via_alert else ""
            lines.append(f"  {at(t.at_ms)} {what} {t.coin} {t.side}: {t.message}{flag}")
        lines += [
            f"Realized: {usd(self.realized)} | Unrealized now: {usd(self.unrealized)} | "
            f"Session P&L: {usd(self.session_pnl)}",
            f"Max drawdown: {usd(self.max_drawdown)}",
            f"Alerts: {self.alerts_fired} fired, {self.alerts_acted} acted, "
            f"{self.alerts_snoozed} snoozed | Policy blocks: {self.policy_blocks}",
            "Numbers only - not advice.",
        ]
        return "\n".join(lines)


def usd(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


class SessionTracker:
    """Counters + the session-P&L curve. Pure apart from the clock."""

    def __init__(self, *, clock_ms: Callable[[], int] | None = None) -> None:
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._reset(unrealized=0.0)

    def _reset(self, unrealized: float) -> None:
        self.started_ms = self._clock_ms()
        self.trade_log: list[TradeRecord] = []
        self.dry_runs = 0
        self.alerts_fired = self.alerts_acted = self.alerts_snoozed = self.policy_blocks = 0
        self.realized = 0.0
        self.unrealized_start = unrealized
        self.unrealized = unrealized
        self.peak = 0.0
        self.max_drawdown = 0.0

    @property
    def session_pnl(self) -> float:
        return self.realized + (self.unrealized - self.unrealized_start)

    def sample(self, unrealized: float, realized: float | None = None) -> None:
        self.unrealized = unrealized
        if realized is not None:
            self.realized = realized
        pnl = self.session_pnl
        self.peak = max(self.peak, pnl)
        self.max_drawdown = min(self.max_drawdown, pnl - self.peak)

    def record_order(self, order: Order, result: OrderResult, *, via_alert: bool = False) -> None:
        if result.status == "blocked":
            self.policy_blocks += 1
            return
        if result.status == "dry_run":
            self.dry_runs += 1
            return
        if not result.ok:
            return
        self.trade_log.append(
            TradeRecord(
                at_ms=self._clock_ms(),
                kind=order.kind,
                coin=order.coin,
                side=order.side,
                fraction=order.fraction if order.kind == "close" else None,
                status=result.status,
                message=result.message,
                via_alert=via_alert,
            )
        )
        if via_alert:
            self.alerts_acted += 1

    def record_alert(self) -> None:
        self.alerts_fired += 1

    def record_snooze(self) -> None:
        self.alerts_snoozed += 1

    def record_block(self) -> None:
        self.policy_blocks += 1

    def snapshot(self, mode: str, *, ended: bool = False) -> SessionSummary:
        return SessionSummary(
            started_ms=self.started_ms,
            ended_ms=self._clock_ms() if ended else None,
            mode=mode,
            trades=len(self.trade_log),
            dry_runs=self.dry_runs,
            realized=self.realized,
            unrealized=self.unrealized,
            session_pnl=self.session_pnl,
            max_drawdown=self.max_drawdown,
            alerts_fired=self.alerts_fired,
            alerts_acted=self.alerts_acted,
            alerts_snoozed=self.alerts_snoozed,
            policy_blocks=self.policy_blocks,
            trade_log=list(self.trade_log),
        )

    def end(self, mode: str) -> SessionSummary:
        summary = self.snapshot(mode, ended=True)
        self._reset(unrealized=self.unrealized)  # the next session starts now
        return summary


class SessionService:
    """The tracker fed with live data: positions from MarketState, realized P&L from the
    exchange adapter. Owns the background sampling loop and the end-of-session record."""

    def __init__(
        self,
        state: MarketState,
        adapter: ExchangeAdapter,
        *,
        store: Store | None = None,
        mode: Callable[[], str] | None = None,
        tracker: SessionTracker | None = None,
    ) -> None:
        self.state = state
        self.adapter = adapter
        self.store = store
        self._mode = mode or (lambda: state.mode)
        self.tracker = tracker or SessionTracker()
        self._realized_at = 0.0
        self.tracker.unrealized_start = self.unrealized_now()

    def unrealized_now(self) -> float:
        return sum(p.upnl for p in self.state.positions)

    async def refresh(self, *, force: bool = False) -> None:
        realized: float | None = None
        loop_now = time.monotonic()
        if force or loop_now - self._realized_at >= REALIZED_REFRESH_S:
            self._realized_at = loop_now
            try:
                realized = await self.adapter.realized_pnl_since(self.tracker.started_ms)
            except Exception as exc:  # keep the last value; the summary says "realized" anyway
                log.warning("session realized P&L refresh failed: %s", exc)
        self.tracker.sample(self.unrealized_now(), realized)

    def snapshot(self) -> SessionSummary:
        self.tracker.sample(self.unrealized_now())
        return self.tracker.snapshot(self._mode())

    async def end(self) -> SessionSummary:
        await self.refresh(force=True)
        summary = self.tracker.end(self._mode())
        self._realized_at = 0.0
        if self.store is not None:
            try:
                await asyncio.to_thread(
                    self.store.log_session,
                    started_ms=summary.started_ms,
                    ended_ms=summary.ended_ms or 0,
                    summary_json=json.dumps(asdict(summary), separators=(",", ":")),
                )
            except Exception:
                log.exception("could not store the session summary")
        log.info(
            "session ended: %d trades, session P&L %s, max drawdown %s",
            summary.trades,
            usd(summary.session_pnl),
            usd(summary.max_drawdown),
        )
        return summary

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("session sampling failed")
            await asyncio.sleep(SAMPLE_S)
