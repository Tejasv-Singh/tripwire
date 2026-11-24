"""Builds the JSON "hud" frame sent to the glasses (schema: docs/ws-protocol.md).

The backend sends *structured* data; the plugin turns it into the ≤46-char × ≤9-line text
(plugin/src/hud.ts). Keeping layout on the plugin means display tweaks never touch the backend.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from tripwire.state import MarketState

PROTOCOL_VERSION = 2  # v2 (Day 2): intents tap/back replace "actions"; frames carry ui + trading
NETWORK = "testnet"

Nav = Literal["next", "prev"]


def clamp_index(index: int, count: int) -> int:
    return 0 if count <= 0 else index % count


def apply_nav(index: int, count: int, action: str) -> int:
    """next/prev wrap around; anything else leaves the index alone."""
    if count <= 0:
        return 0
    if action == "next":
        return (index + 1) % count
    if action == "prev":
        return (index - 1) % count
    return clamp_index(index, count)


def next_send_delay(last_sent: float, now: float, min_interval_s: float) -> float:
    """Seconds to wait before the next frame so frames are at least `min_interval_s` apart."""
    return max(0.0, last_sent + min_interval_s - now)


def resolve_index(state: MarketState, index: int, lock_coin: str | None) -> int:
    """While a menu is open the client is "locked" to one coin, so a position opening or closing
    elsewhere (which reorders the coin-sorted list) cannot swap the position under the menu."""
    if lock_coin is not None:
        for i, p in enumerate(state.positions):
            if p.coin == lock_coin:
                return i
    return clamp_index(index, len(state.positions))


def build_hud_frame(
    state: MarketState,
    *,
    index: int,
    seq: int,
    ui: dict[str, Any] | None = None,
    lock_coin: str | None = None,
    trading: dict[str, Any] | None = None,
    now_ms: int | None = None,
    now_mono: float | None = None,
) -> dict[str, Any]:
    count = len(state.positions)
    idx = resolve_index(state, index, lock_coin)
    selected = state.positions[idx] if count else None
    if lock_coin is not None and selected is not None and selected.coin != lock_coin:
        selected = None  # the locked position is gone; the ui says what happened
    position = selected.to_dict() if selected else None
    age = state.feed_age_s(now_mono)
    ref = state.ref_coin
    ref_mark = state.marks.get(ref)
    return {
        "type": "hud",
        "v": PROTOCOL_VERSION,
        "seq": seq,
        "ts": int(time.time() * 1000) if now_ms is None else now_ms,
        "network": NETWORK,
        "feed": {
            "connected": state.connected,
            "age_ms": None if age is None else int(age * 1000),
        },
        "account": bool(state.account),
        "watch": state.watch,  # positions are someone else's: read-only, menu disabled
        "address": state.account_short,
        "view": {"index": idx, "count": count},
        "position": position,
        "ref": {
            "coin": ref,
            "mid": state.mids.get(ref),
            "mark": ref_mark if ref_mark is not None else state.mids.get(ref),
            "mark_source": "markPx" if ref_mark is not None else "mid",
        },
        "ui": ui or {"mode": "hud"},
        "trading": trading or {"enabled": False, "status": "off"},
    }
