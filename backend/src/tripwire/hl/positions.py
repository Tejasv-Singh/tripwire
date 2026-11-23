"""Pure, side-effect-free normalisation of Hyperliquid positions plus the risk math.

Price sources (see docs/BUILD_LOG.md, "Price sources"):
  * ``mid``  -- latest mid price (allMids, refreshed by activeAssetCtx.midPx). Drives live uPnL.
  * ``mark`` -- activeAssetCtx.markPx. Liquidation triggers on mark, so liq distance uses it.
    Falls back to ``mid`` when no markPx has arrived; ``mark_source`` says which one was used.

Formulas (all in percent):
  upnl         = szi * (mid - entry)                 # szi is signed: +long / -short
  upnl_pct     = upnl / (|szi| * entry) * 100        # on ENTRY NOTIONAL (unlevered move)
  liq_dist_pct = (mark - liq) / mark * 100   LONG    # >0 = buffer left, <=0 = at/through liq
               = (liq - mark) / mark * 100   SHORT
TODO(Day 2): also expose ROE on margin (upnl / margin used) as a second figure next to upnl_pct.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

Side = Literal["LONG", "SHORT"]
MarkSource = Literal["markPx", "mid"]


@dataclass(frozen=True, slots=True)
class Position:
    coin: str
    side: Side
    size: float  # absolute size in coin units
    entry: float
    mark: float  # price used for liquidation distance (markPx, or mid fallback)
    upnl: float
    upnl_pct: float
    liq_px: float | None  # None when the exchange reports no liquidation price
    liq_dist_pct: float | None
    leverage: float
    # extras beyond the spec'd fields, kept for transparency on the wire
    mid: float
    mark_source: MarkSource
    leverage_type: str  # "cross" | "isolated"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def to_float(value: Any) -> float | None:
    """Hyperliquid sends numbers as strings ("3412.5") and sometimes null. Returns None if unusable."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def side_from_szi(szi: float) -> Side:
    return "LONG" if szi > 0 else "SHORT"


def compute_upnl(szi: float, entry: float, mid: float) -> float:
    return szi * (mid - entry)


def compute_upnl_pct(upnl: float, size: float, entry: float) -> float:
    notional = abs(size) * entry
    return 0.0 if notional == 0 else upnl / notional * 100.0


def liq_distance_pct(side: Side, mark: float, liq_px: float | None) -> float | None:
    if liq_px is None or mark <= 0:
        return None
    if side == "LONG":
        return (mark - liq_px) / mark * 100.0
    return (liq_px - mark) / mark * 100.0


def pick_mark(mark_px: float | None, mid: float | None) -> tuple[float | None, MarkSource]:
    """Prefer markPx (what liquidation uses); fall back to mid."""
    if mark_px is not None and mark_px > 0:
        return mark_px, "markPx"
    return mid, "mid"


def normalize_position(
    asset_position: Mapping[str, Any],
    mids: Mapping[str, float],
    marks: Mapping[str, float],
) -> Position | None:
    """Turn one ``clearinghouseState.assetPositions[i]`` entry into a Position.

    Returns None for flat (szi == 0) or malformed entries.
    """
    raw = asset_position.get("position")
    if not isinstance(raw, Mapping):
        return None
    coin = raw.get("coin")
    szi = to_float(raw.get("szi"))
    entry = to_float(raw.get("entryPx"))
    if not isinstance(coin, str) or not szi or entry is None:
        return None

    size = abs(szi)
    side = side_from_szi(szi)

    # mid for uPnL: live mid -> markPx -> exchange's own positionValue / size (last resort)
    mid = mids.get(coin)
    if mid is None:
        mid = marks.get(coin)
    if mid is None:
        pos_value = to_float(raw.get("positionValue"))
        mid = pos_value / size if pos_value else entry

    mark, mark_source = pick_mark(marks.get(coin), mid)
    assert mark is not None  # mid is always set above

    lev = raw.get("leverage") if isinstance(raw.get("leverage"), Mapping) else {}
    leverage = to_float(lev.get("value")) or 0.0
    leverage_type = str(lev.get("type", "cross"))

    liq_px = to_float(raw.get("liquidationPx"))
    upnl = compute_upnl(szi, entry, mid)
    return Position(
        coin=coin,
        side=side,
        size=size,
        entry=entry,
        mark=mark,
        upnl=upnl,
        upnl_pct=compute_upnl_pct(upnl, size, entry),
        liq_px=liq_px,
        liq_dist_pct=liq_distance_pct(side, mark, liq_px),
        leverage=leverage,
        mid=mid,
        mark_source=mark_source,
        leverage_type=leverage_type,
    )


def normalize_positions(
    asset_positions: list[Any],
    mids: Mapping[str, float],
    marks: Mapping[str, float],
) -> list[Position]:
    """All open positions, sorted by coin so next/prev navigation order is stable."""
    out = [
        p
        for ap in asset_positions
        if isinstance(ap, Mapping) and (p := normalize_position(ap, mids, marks)) is not None
    ]
    return sorted(out, key=lambda p: p.coin)
