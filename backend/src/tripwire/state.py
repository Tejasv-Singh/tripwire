"""In-memory market state shared by the Hyperliquid feed (writer) and HUD sockets (readers).

Everything runs on the single asyncio event loop, so there are no locks. Writers call the
``update_*`` methods; each returns True and notifies listeners only when something the HUD shows
actually changed (a mid for a coin nobody watches does not wake every client).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from tripwire.hl.positions import Position, normalize_positions, to_float

log = logging.getLogger(__name__)

Listener = Callable[[], None]


class MarketState:
    def __init__(self, *, ref_coin: str = "ETH", account: str = "") -> None:
        self.ref_coin = ref_coin  # always-shown reference coin when there is no position
        self.account = account  # "" -> no clearinghouse subscription, no positions
        self.mids: dict[str, float] = {}
        self.marks: dict[str, float] = {}
        self.asset_positions: list[Any] = []
        self.positions: list[Position] = []
        self.connected = False
        self.last_msg_at: float | None = None  # time.monotonic() of last WS message (any channel)
        self.version = 0
        self._listeners: set[Listener] = set()

    # --- listeners -----------------------------------------------------------------------------
    def add_listener(self, fn: Listener) -> None:
        self._listeners.add(fn)

    def remove_listener(self, fn: Listener) -> None:
        self._listeners.discard(fn)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def _changed(self) -> None:
        self.version += 1
        for fn in list(self._listeners):
            fn()

    # --- derived -------------------------------------------------------------------------------
    def watched_coins(self) -> set[str]:
        return {self.ref_coin} | {p.coin for p in self.positions}

    def feed_age_s(self, now: float | None = None) -> float | None:
        if self.last_msg_at is None:
            return None
        return (time.monotonic() if now is None else now) - self.last_msg_at

    def _recompute_positions(self) -> bool:
        new = normalize_positions(self.asset_positions, self.mids, self.marks)
        if new != self.positions:
            self.positions = new
            return True
        return False

    # --- writers (called by the feed) ----------------------------------------------------------
    def touch(self, now: float | None = None) -> None:
        self.last_msg_at = time.monotonic() if now is None else now

    def set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            self._changed()

    def update_mids(self, mids: Mapping[str, Any]) -> bool:
        """allMids payload: {"ETH": "1721.25", ...} for every coin (~3.5k on testnet).
        All mids are stored (so a newly opened position has a price immediately), but listeners
        are only woken when a *watched* coin's mid moved."""
        watched = self.watched_coins()
        changed = False
        for coin, raw in mids.items():
            v = to_float(raw)
            if v is None or self.mids.get(coin) == v:
                continue
            self.mids[coin] = v
            changed = changed or coin in watched
        if changed:
            self._recompute_positions()
            self._changed()
        return changed

    def update_asset_ctx(self, coin: str, ctx: Mapping[str, Any]) -> bool:
        """activeAssetCtx payload: markPx (liquidation price basis) and midPx (fresher mid)."""
        changed = False
        mark = to_float(ctx.get("markPx"))
        if mark is not None and self.marks.get(coin) != mark:
            self.marks[coin] = mark
            changed = True
        mid = to_float(ctx.get("midPx"))
        if mid is not None and self.mids.get(coin) != mid:
            self.mids[coin] = mid
            changed = True
        if changed:
            self._recompute_positions()
            self._changed()
        return changed

    def update_clearinghouse(self, ch_state: Mapping[str, Any]) -> bool:
        aps = ch_state.get("assetPositions")
        self.asset_positions = list(aps) if isinstance(aps, list) else []
        changed = self._recompute_positions()
        if changed:
            self._changed()
        return changed
