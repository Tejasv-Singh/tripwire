"""End-to-end latency: exchange event -> rendered on the glasses.

  t0  the backend receives a Hyperliquid message that changes what the glasses show
      (MarketState.last_change_ms; Hyperliquid's activeAssetCtx carries no server timestamp, so
      the backend's receive time is the "event" time)
  t1  the plugin's textContainerUpgrade for the frame built from it has completed, and the plugin
      sends {"type":"rendered","seq":N,"src_ms":t0}; the backend notes the arrival time
  latency = t1 - t0, measured on the backend clock only (no phone/laptop clock skew). It includes
  the build/throttle wait (<= 500 ms), the hop to the phone, the BLE render, and the ack's way back.
p50 / p95 over the last WINDOW samples are in /status and /healthz.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

WINDOW = 500
MAX_PLAUSIBLE_MS = 60_000


class LatencyStats:
    def __init__(self, window: int = WINDOW) -> None:
        self.samples: deque[int] = deque(maxlen=window)

    def add_ack(self, src_ms: int, now_ms: int | None = None) -> int | None:
        """Record one render ack; returns the latency, or None if implausible (ignored)."""
        now = int(time.time() * 1000) if now_ms is None else now_ms
        latency = now - src_ms
        if not 0 <= latency <= MAX_PLAUSIBLE_MS:
            return None
        self.samples.append(latency)
        return latency

    def clear(self) -> int:
        """Drop all samples (npm run demo:reset); returns how many there were."""
        n = len(self.samples)
        self.samples.clear()
        return n

    @staticmethod
    def _pct(sorted_vals: list[int], q: float) -> int:
        idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
        return sorted_vals[idx]

    def summary(self) -> dict[str, Any] | None:
        if not self.samples:
            return None
        vals = sorted(self.samples)
        return {
            "n": len(vals),
            "p50_ms": self._pct(vals, 0.50),
            "p95_ms": self._pct(vals, 0.95),
            "max_ms": vals[-1],
        }

    def line(self) -> str:
        s = self.summary()
        if s is None:
            return "Latency (HL event -> glasses render): no samples yet (connect the glasses)"
        return (
            f"Latency (HL event -> glasses render): p50 {s['p50_ms']} ms, "
            f"p95 {s['p95_ms']} ms (n={s['n']})"
        )
