"""Liquidation-distance alerts: the risk engine (pure) and the service that delivers its alerts.

Rule, per position (coin + side), on liq distance measured from markPx (liquidation triggers on
mark; a position whose mark is still the mid fallback is skipped):
  fire     liq_dist_pct <= threshold            and the alert is armed and not snoozed
  re-arm   liq_dist_pct >= threshold + hysteresis   (so a price wobbling around the threshold
                                                     fires once, not on every tick)
  snooze   no alert for `snooze_s` (5 min); after that it fires again if still inside
Changing the threshold (Telegram /threshold) re-arms everything, so a position already inside the
new threshold alerts right away.

Delivery (AlertService):
  glasses connected   every client switches to the "RISK" screen (ActionFlow.raise_alert)
  nobody connected    **Away mode**: a Telegram DM to TELEGRAM_OWNER_ID with a one-line position
                      summary and "Open Tripwire on your glasses to act." Even plugins have no
                      background / push path to the glasses (confirmed by the Even team), so this
                      DM is the only way to reach a trader who isn't wearing them: a core feature.

Alert episodes (Day 4): an episode opens when an alert fires for a position (coin + side) and stays
open until it is
  acted on   an order on that position really changed something (filled / placed; a dry run
             doesn't count: nothing changed on the exchange)
  snoozed    "Snooze 5m" (if it is still inside afterwards, the next fire is a new episode)
  resolved   the position closed, or its distance is back above threshold + hysteresis
While it is open:
  - Away mode sends **one DM per episode**, and never two DMs for the same position within
    DM_MIN_GAP_S (a price wobbling around threshold + hysteresis can't open-close-open its way
    into a DM storm). A /threshold change that re-fires an open episode doesn't DM again.
  - Glasses that connect (or reconnect) open straight to its RISK screen (`active_alert()`),
    rebuilt from the live position so the numbers are current.
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
DM_MIN_GAP_S = 600.0  # Away mode: never two DMs for the same position within 10 minutes
AWAY_INSTRUCTION = "Open Tripwire on your glasses to act."

Key = tuple[str, str]  # (coin, side)


@dataclass(frozen=True, slots=True)
class Alert:
    coin: str
    side: Side
    size: float
    liq_px: float
    liq_dist_pct: float
    mark: float
    threshold_pct: float
    # for the Away-mode DM's one-line position summary (None in older callers / tests)
    entry: float | None = None
    leverage: float | None = None
    upnl: float | None = None

    @classmethod
    def from_position(cls, p: Position, threshold_pct: float) -> Alert:
        assert p.liq_px is not None and p.liq_dist_pct is not None
        return cls(
            p.coin, p.side, p.size, p.liq_px, p.liq_dist_pct, p.mark, threshold_pct,
            entry=p.entry, leverage=p.leverage, upnl=p.upnl,
        )  # fmt: skip

    @property
    def key(self) -> Key:
        return (self.coin, self.side)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_line(self) -> str:
        """One line: ETH-PERP LONG 0.25 @ 1,650 5x | uPnL +$4.25 | liq 1,420: 14.8% from mark
        1,666.3 (alert at 20%)."""
        head = f"{self.coin}-PERP {self.side} {self.size:g}"
        if self.entry is not None:
            head += f" @ {self.entry:,.6g}"
        if self.leverage:
            head += f" {self.leverage:g}x"
        parts = [head]
        if self.upnl is not None:
            parts.append(f"uPnL {'-' if self.upnl < 0 else '+'}${abs(self.upnl):,.2f}")
        parts.append(
            f"liq {self.liq_px:,.6g}: {self.liq_dist_pct:.1f}% from mark {self.mark:,.6g} "
            f"(alert at {self.threshold_pct:g}%)"
        )
        return " | ".join(parts)

    def text(self) -> str:
        """Away-mode Telegram DM (Unicode is fine in Telegram, unlike the glasses font)."""
        return f"▲ RISK - Away mode\n{self.summary_line()}\n{AWAY_INSTRUCTION}"


@dataclass(slots=True)
class Episode:
    """One alert episode for a position: open from the first fire until acted on / snoozed /
    resolved (see the module docstring)."""

    alert: Alert  # the latest fire's numbers
    opened: float  # AlertService clock
    dm_sent: bool = False
    shown_on_glasses: bool = False

    def status(self, now: float) -> dict[str, Any]:
        return {
            "position": f"{self.alert.coin} {self.alert.side}",
            "liq_dist_pct": round(self.alert.liq_dist_pct, 2),
            "open_s": int(now - self.opened),
            "dm_sent": self.dm_sent,
            "shown_on_glasses": self.shown_on_glasses,
        }


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
                alerts.append(Alert.from_position(p, self.threshold_pct))
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

    def reset(self) -> None:
        """Forget every position's armed / snoozed state (npm run demo:reset)."""
        self._keys.clear()
        self.fired = 0

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
        on_fire: Callable[[Alert], None] | None = None,  # session summary counts alerts
    ) -> None:
        self.engine = engine
        self.on_fire = on_fire
        self.adapter = adapter
        self.glasses = glasses
        self.notifier = notifier
        self.store = store
        self._clock = clock
        self.last_delivery: dict[str, Any] | None = None
        self.episodes: dict[Key, Episode] = {}  # open alert episodes (module docstring)
        self._last_dm: dict[Key, float] = {}  # Away-mode DM cooldown per position
        self.positions: list[Position] = []  # as of the last check (fresh numbers for reconnects)
        self.dms_sent = 0
        self.dms_suppressed = 0

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
        now = self.now()
        episode = self.episodes.get(alert.key)
        if episode is None:
            episode = self.episodes[alert.key] = Episode(alert, now)
            if self.on_fire is not None:
                self.on_fire(alert)  # the session counts episodes, not re-fires
        else:
            episode.alert = alert  # e.g. /threshold re-armed an episode that is still open
        shown = self.glasses(alert)
        if shown:
            episode.shown_on_glasses = True
            where = f"glasses ({shown})"
        else:
            where = await self._away_dm(episode, now)
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

    async def _away_dm(self, episode: Episode, now: float) -> str:
        """Away mode: nobody is wearing the glasses. One DM per episode, and per position never
        two within DM_MIN_GAP_S."""
        key = episode.alert.key
        if episode.dm_sent:
            self.dms_suppressed += 1
            return "telegram (already sent for this alert)"
        last = self._last_dm.get(key)
        if last is not None and now - last < DM_MIN_GAP_S:
            self.dms_suppressed += 1
            return f"telegram suppressed (last DM for {key[0]} {key[1]} {int(now - last)}s ago)"
        if self.notifier is not None and await self.notifier.send_owner(episode.alert.text()):
            episode.dm_sent = True
            self._last_dm[key] = now
            self.dms_sent += 1
            return "telegram"
        return "nowhere (no glasses, no Telegram owner)"

    def reset(self, threshold_pct: float) -> dict[str, Any]:
        """Clean slate between demo takes (npm run demo:reset): no open episodes, no DM cooldowns,
        everything re-armed and unsnoozed, threshold back to `threshold_pct` (the .env value).
        Positions and the exchange are not touched."""
        cleared = {"episodes": len(self.episodes), "cooldowns": len(self._last_dm)}
        self.engine.reset()
        self.set_threshold(threshold_pct)
        self.episodes.clear()
        self._last_dm.clear()
        self.last_delivery = None
        self.dms_sent = self.dms_suppressed = 0
        return {**cleared, "threshold_pct": threshold_pct}

    def close_episode(self, coin: str, side: str, why: str) -> bool:
        """End an open episode (acted on / snoozed / resolved). True if one was open."""
        episode = self.episodes.pop((coin, side), None)
        if episode is not None:
            log.info("alert episode %s %s closed: %s", coin, side, why)
        return episode is not None

    def _resolve(self, positions: list[Position]) -> None:
        """Close episodes whose position is gone or back above threshold + hysteresis."""
        live: dict[Key, Position] = {(p.coin, p.side): p for p in positions}
        clear = self.engine.threshold_pct + self.engine.hysteresis_pct
        for key in list(self.episodes):
            p = live.get(key)
            if p is None:
                self.close_episode(*key, why="position closed")
            elif p.liq_dist_pct is not None and p.liq_dist_pct >= clear:
                self.close_episode(*key, why=f"recovered ({p.liq_dist_pct:.1f}% >= {clear:g}%)")

    def active_alert(self) -> Alert | None:
        """The most urgent open episode, with the live position's numbers when available.
        Glasses that connect open straight to it (ws.py)."""
        live: dict[Key, Position] = {(p.coin, p.side): p for p in self.positions}
        best: Alert | None = None
        for key, episode in self.episodes.items():
            p = live.get(key)
            alert = episode.alert
            if p is not None and p.liq_px is not None and p.liq_dist_pct is not None:
                alert = Alert.from_position(p, self.engine.threshold_pct)
            if best is None or alert.liq_dist_pct < best.liq_dist_pct:
                best = alert
        return best

    def status(self, now: float | None = None) -> dict[str, Any]:
        t = self.now() if now is None else now
        return {
            **self.engine.status(t),
            "open": [e.status(t) for e in self.episodes.values()],
            "dms_sent": self.dms_sent,
            "dms_suppressed": self.dms_suppressed,
            "last": self.last_delivery,
        }

    async def check(self, positions: list[Position]) -> list[Alert]:
        self.positions = positions
        alerts = self.engine.evaluate(positions, self.now())
        self._resolve(positions)
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
