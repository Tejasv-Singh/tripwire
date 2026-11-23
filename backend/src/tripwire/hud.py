"""Builds the JSON "hud" frame sent to the glasses (schema: docs/ws-protocol.md).

The backend sends *structured* data; the plugin turns it into the ≤46-char × ≤9-line text
(plugin/src/hud.ts). Keeping layout on the plugin means display tweaks never touch the backend.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from tripwire.state import MarketState

PROTOCOL_VERSION = 1
NETWORK = "testnet"

Action = Literal["next", "prev", "actions"]


def clamp_index(index: int, count: int) -> int:
    return 0 if count <= 0 else index % count


def apply_nav(index: int, count: int, action: Action) -> int:
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


def build_hud_frame(
    state: MarketState,
    *,
    index: int,
    seq: int,
    now_ms: int | None = None,
    now_mono: float | None = None,
) -> dict[str, Any]:
    count = len(state.positions)
    idx = clamp_index(index, count)
    position = state.positions[idx].to_dict() if count else None
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
        "view": {"index": idx, "count": count},
        "position": position,
        "ref": {
            "coin": ref,
            "mid": state.mids.get(ref),
            "mark": ref_mark if ref_mark is not None else state.mids.get(ref),
            "mark_source": "markPx" if ref_mark is not None else "mid",
        },
    }
