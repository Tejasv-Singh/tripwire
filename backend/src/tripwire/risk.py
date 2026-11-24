"""Liquidation-distance alerts: the risk engine (pure) and the service that delivers its alerts.

Rule, per position (coin + side), on liq distance measured from markPx (liquidation triggers on
mark; a position whose mark is still the mid fallback is skipped):
  fire     liq_dist_pct <= threshold            and the alert is armed and not snoozed
  re-arm   liq_dist_pct >= threshold + hysteresis   (so a price wobbling around the threshold
                                                     fires once, not on every tick)
  snooze   no alert for `snooze_s` (5 min); after that it fires again if still inside
Changing the threshold (Telegram /threshold) re-arms everything, so a position already inside the
new threshold alerts right away.

Delivery (AlertService): glasses connected -> every client switches to the "RISK" screen
(ActionFlow.raise_alert); nobody connected -> a Telegram DM to TELEGRAM_OWNER_ID.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from tripwire.db import Store
from tripwire.exchanges.base import ExchangeAdapter
from tripwire.hl.positions import Position, Side

log = logging.getLogger(__name__)

THRESHOLD_KEY = "alert_liq_pct"
TICK_S = 5.0  # re-evaluate at least this often (snooze expiry without a price change)


@dataclass(frozen=True, slots=True)
class Alert:
    coin: str
    side: Side
    size: float
    liq_px: float
    liq_dist_pct: float
    mark: float
    threshold_pct: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def text(self) -> str:
        """Telegram DM body (Unicode is fine in Telegram, unlike the glasses font)."""
        return (
            f"▲ RISK  {self.coin}-PERP {self.side} {self.size:g}\n"
            f"Liquidation {self.liq_px:,.6g} is {self.liq_dist_pct:.1f}% from mark "
            f"{self.mark:,.6g} (alert at {self.threshold_pct:g}%).\n"
            "Glasses not connected - open Tripwire to act (Move SL / Close), or send /status."
        )


@dataclass(slots=True)
class _KeyState:
    armed: bool = True
    snoozed_until: float = 0.0


class RiskEngine:
    def __init__(
        self, *, threshold_pct: float = 5.0, hysteresis_pct: float = 1.0, snooze_s: float = 300.0
    ) -> None:
        self.threshold_pct = threshold_pct
        self.hysteresis_pct = hysteresis_pct
        self.snooze_s = snooze_s
        self._keys: dict[tuple[str, str], _KeyState] = {}
        self.fired = 0

    def evaluate(self, positions: list[Position], now: float) -> list[Alert]:
        alerts: list[Alert] = []
        seen: set[tuple[str, str]] = set()
        for p in positions:
            if p.liq_px is None or p.liq_dist_pct is None or p.mark_source != "markPx":
                continue
            key: tuple[str, str] = (p.coin, p.side)
            seen.add(key)
            st = self._keys.setdefault(key, _KeyState())
            dist = p.liq_dist_pct
            if dist >= self.threshold_pct + self.hysteresis_pct:
                st.armed = True
                continue
            if dist <= self.threshold_pct and st.armed and now >= st.snoozed_until:
                st.armed = False
                self.fired += 1
                alerts.append(
                    Alert(p.coin, p.side, p.size, p.liq_px, dist, p.mark, self.threshold_pct)
                )
        for key in set(self._keys) - seen:  # position closed: forget it
            del self._keys[key]
        return alerts

    def snooze(self, coin: str, side: str, now: float) -> None:
        st = self._keys.setdefault((coin, side), _KeyState())
        st.snoozed_until = now + self.snooze_s
        st.armed = True  # fires again after the snooze if still inside the threshold

    def set_threshold(self, pct: float) -> None:
        self.threshold_pct = pct
        for st in self._keys.values():
            st.armed = True

    def status(self, now: float) -> dict[str, Any]:
        return {
            "threshold_pct": self.threshold_pct,
            "hysteresis_pct": self.hysteresis_pct,
            "snooze_s": self.snooze_s,
            "fired": self.fired,
            "inside": sorted(f"{c} {s}" for (c, s), st in self._keys.items() if not st.armed),
            "snoozed": sorted(
                f"{c} {s}" for (c, s), st in self._keys.items() if st.snoozed_until > now
            ),
        }


class Notifier(Protocol):
    async def send_owner(self, text: str) -> bool: ...


GlassesSink = Callable[[Alert], int]  # raise on every connected client; returns how many


class AlertService:
    """Feeds the engine from the exchange's positions stream and delivers what fires."""

    def __init__(
        self,
        engine: RiskEngine,
        adapter: ExchangeAdapter,
        *,
        glasses: GlassesSink,
        notifier: Notifier | None,
        store: Store | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.engine = engine
        self.adapter = adapter
        self.glasses = glasses
        self.notifier = notifier
        self.store = store
        self._clock = clock
        self.last_delivery: dict[str, Any] | None = None

    def now(self) -> float:
        return self._clock() if self._clock else asyncio.get_running_loop().time()

    def load_threshold(self) -> None:
        if self.store is None:
            return
        saved = self.store.kv_get(THRESHOLD_KEY)
        if saved:
            try:
                self.engine.set_threshold(float(saved))
            except ValueError:
                log.warning("ignoring bad saved alert threshold %r", saved)

    def set_threshold(self, pct: float) -> None:
        self.engine.set_threshold(pct)
        if self.store is not None:
            self.store.kv_set(THRESHOLD_KEY, f"{pct:g}")
        log.info("alert threshold set to %g%% (re-armed)", pct)

    async def deliver(self, alert: Alert) -> str:
        shown = self.glasses(alert)
        if shown:
            where = f"glasses ({shown})"
        elif self.notifier is not None and await self.notifier.send_owner(alert.text()):
            where = "telegram"
        else:
            where = "nowhere (no glasses, no Telegram owner)"
        self.last_delivery = {"alert": alert.to_dict(), "delivered_to": where}
        log.warning(
            "RISK alert %s %s liq %.1f%% from mark (threshold %g%%) -> %s",
            alert.coin,
            alert.side,
            alert.liq_dist_pct,
            alert.threshold_pct,
            where,
        )
        return where

    async def check(self, positions: list[Position]) -> list[Alert]:
        alerts = self.engine.evaluate(positions, self.now())
        for alert in alerts:
            await self.deliver(alert)
        return alerts

    async def run(self) -> None:
        """Forever: re-evaluate on every positions update, and at least every TICK_S."""
        stream = self.adapter.stream_positions()
        positions: list[Position] = []
        pending: asyncio.Future[list[Position]] = asyncio.ensure_future(anext(stream))
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=TICK_S)
                if pending in done:
                    positions = pending.result()
                    pending = asyncio.ensure_future(anext(stream))
                try:
                    await self.check(positions)
                except Exception:  # an alert bug must never stop the loop
                    log.exception("risk check failed")
        finally:
            pending.cancel()
            await stream.aclose()  # type: ignore[attr-defined]
