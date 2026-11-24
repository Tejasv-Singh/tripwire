"""Day-3 audit of order payloads against hyperliquid-python-sdk's own examples
(examples/basic_tpsl.py, examples/rounding.py): stop-loss trigger shape, slippage room,
price ticks and lot sizes per asset."""

from __future__ import annotations

import random

import pytest
from hyperliquid.utils.signing import order_request_to_order_wire

from tripwire.exchanges.hyperliquid import _wire
from tripwire.orders import OpenOrder, StopOrder
from tripwire.trading import (
    TRIGGER_SLIPPAGE,
    is_valid_px,
    is_valid_sz,
    round_px,
    round_size_down,
    trigger_limit_px,
)

from .test_trading import LEVERAGE_OK, PLACED, Poster, adapter

# szDecimals read from testnet /info meta on 2026-11-25: BTC 5, ETH 4, SOL 2, DOGE 0.
SZ = {"BTC": 5, "ETH": 4, "SOL": 2, "DOGE": 0}


# --- prices ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("px", "szd", "expected"),
    [
        (3319.68, 4, 3319.7),  # ETH: 5 significant figures
        (1234.56, 4, 1234.6),  # rounding.py: 1234.5 valid, 1234.56 not
        (96512.34, 5, 96512.0),  # BTC under 100k: 5 sig figs
        (105432.7, 5, 105433.0),  # BTC above 100k: any integer is valid (rounding.py)
        (0.0012345, 0, 0.001234),  # rounding.py: 0.001234 valid, 0.0012345 not (6 decimals)
        (0.0556271, 0, 0.055627),  # DOGE: <= 6 - 0 decimals
        (150.236, 2, 150.24),  # SOL: <= 4 decimals, 5 sig figs wins
        (1.2345678, 2, 1.2346),  # rounding.py's OP example (szDecimals 2 -> 4 decimals)
        (12.345678, 5, 12.3),  # szDecimals 5 -> only 1 decimal
    ],
)
def test_round_px_matches_the_sdk_rules(px: float, szd: int, expected: float) -> None:
    assert round_px(px, szd) == expected
    assert is_valid_px(expected, szd)


def test_unrounded_prices_are_flagged_invalid() -> None:
    assert not is_valid_px(1234.56, 4) and not is_valid_px(0.0012345, 0) and not is_valid_px(0, 4)
    assert is_valid_px(123456.0, 5)  # integer above 100k: valid despite 6 significant figures


def test_rounding_always_produces_a_valid_price() -> None:
    rng = random.Random(25)
    for _ in range(2000):
        szd = rng.randint(0, 5)
        tick = 10.0 ** -(6 - szd)
        px = tick * 10 ** rng.uniform(0, 10)  # from one tick up to far above 100k
        assert is_valid_px(round_px(px, szd), szd), (px, szd)


def test_a_price_below_the_smallest_tick_is_refused_not_zeroed() -> None:
    # found by the property test above: 0.004 with szDecimals 5 used to round to 0.0
    with pytest.raises(ValueError, match="below the smallest tick"):
        round_px(0.004, 5)
    assert not is_valid_px(0.004, 5)


# --- sizes -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("coin", "size", "expected"),
    [
        ("BTC", 0.000155, 0.00015),  # 5 decimals, down
        ("ETH", 15 / 1702.2, 0.0088),  # a $15 ETH order
        ("SOL", 0.2199, 0.21),
        ("DOGE", 271.9, 271.0),  # whole coins only
    ],
)
def test_sizes_round_down_to_each_assets_lot(coin: str, size: float, expected: float) -> None:
    got = round_size_down(size, SZ[coin])
    assert got == expected and is_valid_sz(got, SZ[coin]) and got <= size


def test_size_validity() -> None:
    assert is_valid_sz(0.0088, 4) and not is_valid_sz(0.00881, 4) and not is_valid_sz(0, 4)


# --- trigger (stop-loss / take-profit) orders ---------------------------------------------------


def test_trigger_limit_leaves_slippage_room_on_the_right_side() -> None:
    assert TRIGGER_SLIPPAGE == 0.10
    assert trigger_limit_px(3412.0, is_buy=False, sz_decimals=4) == 3070.8  # sell stop: lower
    assert trigger_limit_px(1800.0, is_buy=True, sz_decimals=4) == 1980.0  # buy stop: higher
    assert trigger_limit_px(96500.0, is_buy=False, sz_decimals=5) == 86850.0
    assert is_valid_px(trigger_limit_px(0.0556, is_buy=False, sz_decimals=0), 0)


def test_our_stop_wire_equals_the_sdk_example_shape() -> None:
    """basic_tpsl.py: exchange.order(coin, not is_buy, sz, sl_px, {"trigger": {"triggerPx":
    trigger_px, "isMarket": True, "tpsl": "sl"}}, reduce_only=True) with sl_px past trigger_px."""
    sdk_request = {
        "coin": "ETH",
        "is_buy": False,
        "sz": 0.5,
        "limit_px": 3070.8,
        "order_type": {"trigger": {"triggerPx": 3412.0, "isMarket": True, "tpsl": "sl"}},
        "reduce_only": True,
    }
    sdk_wire = order_request_to_order_wire(sdk_request, 4)  # type: ignore[arg-type]
    ours = _wire(4, False, 0.5, 3070.8, sdk_request["order_type"], True)  # type: ignore[arg-type]
    assert ours == sdk_wire
    assert ours == {
        "a": 4, "b": False, "p": "3070.8", "s": "0.5", "r": True,
        "t": {"trigger": {"isMarket": True, "triggerPx": "3412", "tpsl": "sl"}},
    }  # fmt: skip


async def test_set_sl_payload_long_and_short() -> None:
    poster = Poster(PLACED)
    await adapter(poster).set_sl(StopOrder("ETH", "LONG", 3412.0, None, None, "SL to breakeven"))
    (o,) = poster.actions[0]["orders"]
    assert poster.actions[0]["grouping"] == "positionTpsl"
    assert (o["b"], o["r"], o["s"], o["p"]) == (False, True, "0.5", "3070.8")
    assert o["t"]["trigger"] == {"isMarket": True, "triggerPx": "3412", "tpsl": "sl"}


async def test_open_legs_carry_slippage_room_too() -> None:
    poster = Poster(LEVERAGE_OK, PLACED)
    await adapter(poster).place_order(OpenOrder("ETH", "LONG", 100.0, 3.0, 3400.0, 3300.0, 3600.0))
    entry, sl, tp = poster.actions[1]["orders"]
    assert (sl["t"]["trigger"]["triggerPx"], sl["p"]) == ("3300", "2970")  # sell stop: -10 %
    assert (tp["t"]["trigger"]["triggerPx"], tp["p"]) == ("3600", "3240")  # sell TP: -10 %
    assert entry["t"] == {"limit": {"tif": "Gtc"}} and entry["p"] == "3400"
