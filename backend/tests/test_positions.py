"""Normalisation + risk math for Hyperliquid positions."""

from __future__ import annotations

from typing import Any

import pytest

from tripwire.hl.positions import (
    compute_upnl,
    compute_upnl_pct,
    liq_distance_pct,
    normalize_position,
    normalize_positions,
    pick_mark,
    to_float,
)


def raw_position(
    coin: str = "ETH",
    szi: str = "0.5",
    entry: str = "3412.0",
    liq: str | None = "2980.0",
    lev: int = 5,
    lev_type: str = "cross",
) -> dict[str, Any]:
    """Shape copied from a real testnet clearinghouseState.assetPositions entry (numbers as str)."""
    return {
        "type": "oneWay",
        "position": {
            "coin": coin,
            "szi": szi,
            "entryPx": entry,
            "positionValue": "1747.2",
            "unrealizedPnl": "41.2",
            "returnOnEquity": "0.1207",
            "liquidationPx": liq,
            "leverage": {"type": lev_type, "value": lev},
            "marginUsed": "349.44",
            "maxLeverage": 25,
            "cumFunding": {"allTime": "0.0", "sinceOpen": "0.0", "sinceChange": "0.0"},
        },
    }


# --- primitive math ------------------------------------------------------------------------------


def test_to_float_handles_strings_none_and_garbage() -> None:
    assert to_float("3412.5") == 3412.5
    assert to_float(7) == 7.0
    assert to_float(None) is None
    assert to_float("") is None
    assert to_float("n/a") is None


def test_upnl_long_and_short_sign() -> None:
    assert compute_upnl(0.5, 3412.0, 3494.4) == pytest.approx(41.2)
    assert compute_upnl(-0.5, 3412.0, 3494.4) == pytest.approx(-41.2)
    assert compute_upnl(-2.0, 100.0, 90.0) == pytest.approx(20.0)


def test_upnl_pct_is_on_entry_notional_matching_the_mock() -> None:
    # Mock HUD: 0.50 @ 3,412 -> uPnL +$41.20 (+2.4%)  => 41.20 / 1706 = 2.415%
    assert compute_upnl_pct(41.2, 0.5, 3412.0) == pytest.approx(2.415, abs=1e-3)
    assert compute_upnl_pct(5.0, 0.0, 3412.0) == 0.0  # no notional -> 0, not ZeroDivisionError


def test_liq_distance_long_short_and_missing() -> None:
    assert liq_distance_pct("LONG", 3494.4, 2980.0) == pytest.approx(14.7207, abs=1e-3)
    assert liq_distance_pct("SHORT", 100.0, 125.0) == pytest.approx(25.0)
    # through liquidation -> non-positive
    assert liq_distance_pct("LONG", 2900.0, 2980.0) == pytest.approx(-2.7586, abs=1e-3)
    assert liq_distance_pct("LONG", 3494.4, None) is None
    assert liq_distance_pct("LONG", 0.0, 2980.0) is None


def test_pick_mark_prefers_markpx_then_mid() -> None:
    assert pick_mark(3500.0, 3494.4) == (3500.0, "markPx")
    assert pick_mark(None, 3494.4) == (3494.4, "mid")
    assert pick_mark(0.0, 3494.4) == (3494.4, "mid")


# --- normalisation -------------------------------------------------------------------------------


def test_normalize_long_uses_mid_for_upnl_and_mark_for_liq() -> None:
    p = normalize_position(raw_position(), mids={"ETH": 3494.4}, marks={"ETH": 3500.0})
    assert p is not None
    assert (p.coin, p.side, p.size, p.entry, p.leverage) == ("ETH", "LONG", 0.5, 3412.0, 5.0)
    assert p.mid == 3494.4
    assert p.upnl == pytest.approx(41.2)  # from mid, not markPx
    assert p.upnl_pct == pytest.approx(2.415, abs=1e-3)
    assert (p.mark, p.mark_source) == (3500.0, "markPx")
    assert p.liq_px == 2980.0
    assert p.liq_dist_pct == pytest.approx((3500.0 - 2980.0) / 3500.0 * 100)
    assert p.leverage_type == "cross"


def test_normalize_short_position() -> None:
    p = normalize_position(
        raw_position(coin="BTC", szi="-0.01", entry="60000", liq="66000", lev=10),
        mids={"BTC": 61000.0},
        marks={"BTC": 61000.0},
    )
    assert p is not None
    assert (p.side, p.size) == ("SHORT", 0.01)
    assert p.upnl == pytest.approx(-10.0)
    assert p.liq_dist_pct == pytest.approx((66000 - 61000) / 61000 * 100)


def test_mark_falls_back_to_mid_when_markpx_missing() -> None:
    p = normalize_position(raw_position(), mids={"ETH": 3494.4}, marks={})
    assert p is not None
    assert (p.mark, p.mark_source) == (3494.4, "mid")


def test_mid_falls_back_to_markpx_then_position_value() -> None:
    p = normalize_position(raw_position(), mids={}, marks={"ETH": 3500.0})
    assert p is not None and p.mid == 3500.0
    p = normalize_position(raw_position(), mids={}, marks={})
    assert p is not None and p.mid == pytest.approx(1747.2 / 0.5)


def test_missing_liquidation_price_gives_none_distance() -> None:
    p = normalize_position(raw_position(liq=None), mids={"ETH": 3494.4}, marks={})
    assert p is not None
    assert p.liq_px is None and p.liq_dist_pct is None


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "oneWay", "position": {"coin": "ETH", "szi": "0.0", "entryPx": "3412"}},
        {"type": "oneWay", "position": {"coin": "ETH", "szi": "0.5", "entryPx": None}},
        {"type": "oneWay", "position": {"szi": "0.5", "entryPx": "3412"}},
        {"type": "oneWay"},
        {"type": "oneWay", "position": "garbage"},
    ],
)
def test_flat_or_malformed_entries_are_skipped(entry: dict[str, Any]) -> None:
    assert normalize_position(entry, mids={"ETH": 1.0}, marks={}) is None


def test_normalize_positions_sorts_by_coin_and_drops_flat() -> None:
    positions = normalize_positions(
        [
            raw_position(coin="SOL", szi="10", entry="150", liq="120"),
            raw_position(coin="ETH"),
            raw_position(coin="BTC", szi="0.0"),
            "not-a-dict",
        ],
        mids={"ETH": 3494.4, "SOL": 155.0},
        marks={},
    )
    assert [p.coin for p in positions] == ["ETH", "SOL"]


def test_to_dict_contains_every_spec_field() -> None:
    p = normalize_position(raw_position(), mids={"ETH": 3494.4}, marks={"ETH": 3500.0})
    assert p is not None
    spec = {
        "coin", "side", "size", "entry", "mark", "upnl", "upnl_pct",
        "liq_px", "liq_dist_pct", "leverage",
    }  # fmt: skip
    assert spec <= set(p.to_dict())
