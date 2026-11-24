"""Forwarded-message parser (item 6): the formats it must read, and everything it must refuse."""

from __future__ import annotations

import pytest

from tripwire.drafts import DraftError, OrderDraft, parse_draft, parse_number

COINS = {"BTC", "ETH", "SOL", "KPEPE", "DOGE"}


def p(text: str, **kw: object) -> OrderDraft:
    return parse_draft(text, known_coins=kw.pop("known", COINS), **kw)  # type: ignore[arg-type]


# --- what it reads -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "coin", "side", "entry", "sl", "tp"),
    [
        ("BTC long 96.5k sl 95k tp 99k", "BTC", "LONG", 96_500, 95_000, 99_000),
        ("ETH short @3400 SL 3480 TP 3200", "ETH", "SHORT", 3_400, 3_480, 3_200),
        ("eth SHORT @ 3400 sl 3480", "ETH", "SHORT", 3_400, 3_480, None),
        ("btc buy 96,500 stop 95,000 target 99,000", "BTC", "LONG", 96_500, 95_000, 99_000),
        ("Long BTC entry 96.5K stop-loss 95K take-profit 1.2m",
         "BTC", "LONG", 96_500, 95_000, 1_200_000),
        ("SOL sell at 150 sl 160", "SOL", "SHORT", 150, 160, None),
        ("BTC-PERP long 96500 sl 95000", "BTC", "LONG", 96_500, 95_000, None),
        ("btcusdt long 96500 sl 95000", "BTC", "LONG", 96_500, 95_000, None),
        ("ETH long market sl 3300 tp 3700", "ETH", "LONG", None, 3_300, 3_700),
        ("ETH long sl 3300", "ETH", "LONG", None, 3_300, None),  # no entry = market
        ("doge long .055 sl .05", "DOGE", "LONG", 0.055, 0.05, None),
        ("BTC long 96.5k, SL: 95k, TP: 99k", "BTC", "LONG", 96_500, 95_000, 99_000),
        ("kPEPE long 0.0123 sl 0.011", "KPEPE", "LONG", 0.0123, 0.011, None),
    ],
)  # fmt: skip
def test_reads_common_formats(
    text: str, coin: str, side: str, entry: float | None, sl: float | None, tp: float | None
) -> None:
    d = p(text)
    assert (d.coin, d.side, d.entry_px, d.sl_px, d.tp_px) == (coin, side, entry, sl, tp)


def test_leverage_notional_and_defaults() -> None:
    d = p("BTC long 96.5k sl 95k tp 99k")
    assert (d.notional_usd, d.leverage, d.defaults) == (100.0, 1.0, ("notional", "leverage"))
    d = p("sol buy 150 sl 140 5x $200")
    assert (d.notional_usd, d.leverage, d.defaults) == (200.0, 5.0, ())
    assert p("ETH long 3400 sl 3300 lev 3 size 150").leverage == 3.0
    assert p("ETH long 3400 sl 3300 lev 3 size 150").notional_usd == 150.0
    assert p("ETH long 3400 sl 3300 250 usd 2 x").notional_usd == 250.0
    assert p("ETH long 3400 sl 3300 250usdc").notional_usd == 250.0
    assert p("ETH long 3400 sl 3300 250$").notional_usd == 250.0
    # regression: a trailing "$" belongs to the number it touches, not the one before a space
    d = p("BTC long 96.5k sl 95k tp 99k $1000")
    assert (d.tp_px, d.notional_usd) == (99_000, 1_000)
    assert p("ETH long 3400 sl 3300", default_notional=50, default_leverage=2).leverage == 2.0


def test_chatter_is_ignored_once_the_coin_list_is_known() -> None:
    d = p("lfg BTC long 96.5k sl 95k tp 99k pls")
    assert d.coin == "BTC"
    with pytest.raises(DraftError, match=r"more than one coin \(LFG, BTC, PLS\)"):
        parse_draft("lfg BTC long 96.5k sl 95k tp 99k pls")  # no coin list: can't tell


def test_to_order_and_summary() -> None:
    d = p("BTC long 96.5k sl 95k tp 99k 3x $150")
    o = d.to_order()
    assert (o.coin, o.side, o.notional_usd, o.leverage, o.entry_px, o.sl_px, o.tp_px) == (
        "BTC", "LONG", 150.0, 3.0, 96_500.0, 95_000.0, 99_000.0,
    )  # fmt: skip
    assert d.summary() == "BTC LONG $150 3x @ 96,500  SL 95,000  TP 99,000"
    assert p("ETH long market").summary() == "ETH LONG $100 1x market  no SL"


def test_parse_number() -> None:
    assert parse_number("96.5k") == 96_500
    assert parse_number("96,500.5") == 96_500.5
    assert parse_number("1.2M") == 1_200_000
    assert parse_number("$200") == 200
    with pytest.raises(DraftError):
        parse_number("0")


# --- what it refuses (ambiguous or malformed) ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty message"),
        ("   ", "empty message"),
        ("long 96.5k sl 95k", "no coin found"),
        ("BTC 96.5k sl 95k", "say long or short"),
        ("BTC long short 96.5k sl 95k", "both long and short"),
        ("BTC ETH long 96.5k sl 95k", r"more than one coin \(BTC, ETH\)"),
        ("BTC long 96.5k 95k", "which price is 95k"),  # unlabelled second number
        ("BTC long 96.5k sl 95k 99k", "which price is 99k"),
        ("BTC long sl 95k sl 94k", r"two different values for sl \(95000 and 94k\)"),
        ("BTC long 96.5k @97k sl 95k", "two different values for entry"),
        ("BTC long market @97k sl 95k", r"two different values for entry \(market and 97k\)"),
        ("BTC long 96.5k sl", "'sl' has no number after it"),
        ("BTC long 96.5k sl 95k 2.5x", "leverage must be a whole number"),
        ("BTC long 96.5k sl 95k lev 0", "'0' must be greater than 0"),
        ("ETH long market 3300", "which price is 3300"),  # market + a bare price: which?
        ("BTC long 0 sl 95k", "'0' must be greater than 0"),
        ("96.5k BTC long", "which price is 96.5k"),  # a number before coin/side
        ("XYZ long 1 sl 0.9", "no coin found"),  # unknown coin (coin list known)
        ("BTC long " + "1 " * 200, "too long"),
    ],
)
def test_refuses_with_a_reason(text: str, reason: str) -> None:
    with pytest.raises(DraftError, match=reason):
        p(text)


def test_sl_on_the_wrong_side_is_left_to_the_policy_guard() -> None:
    d = p("BTC long 96.5k sl 97k")  # parses: the guard explains why it is refused
    assert d.sl_px == 97_000 and d.entry_px == 96_500


def test_missing_sl_is_not_a_parse_error() -> None:
    assert p("BTC long 96.5k").sl_px is None  # the guard says "Opening orders need a stop-loss"
