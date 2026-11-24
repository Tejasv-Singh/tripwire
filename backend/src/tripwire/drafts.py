"""Forwarded-message order drafts: a deterministic parser (no LLM) into a strict schema.

    "BTC long 96.5k sl 95k tp 99k"            -> BTC LONG  entry 96,500  sl 95,000  tp 99,000
    "ETH short @3400 SL 3480 TP 3200"         -> ETH SHORT entry 3,400   sl 3,480   tp 3,200
    "sol buy 150 sl 140 5x $200"              -> SOL LONG  entry 150 sl 140, 5x, $200 notional
    "ETH long market sl 3300"                 -> ETH LONG  market (entry None) sl 3,300

Schema (`OrderDraft`): coin, side, notional_usd, leverage, entry_px (None = market), sl_px, tp_px.
Missing notional / leverage use DRAFT_DEFAULT_NOTIONAL_USD / DRAFT_DEFAULT_LEVERAGE and are
listed in `defaults` so the glasses can say so. A missing SL is NOT an error here: the policy
guard refuses it with its own reason ("Opening orders need a stop-loss").

Rejected as ambiguous (DraftError, reason shown in Telegram):
  no coin / more than one coin; no side / both long and short; a number with no label after the
  entry ("96.5k 95k" -- which is the stop?); the same field twice with different values;
  a fractional leverage; a zero/negative number; an unknown coin (when the coin list is known).
The draft is the user's own instruction, not advice: it goes through the policy guard and the
two-tap confirm like everything else.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from tripwire.hl.positions import Side
from tripwire.orders import OpenOrder

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?[km]?|\d+(?:\.\d+)?[km]?|\.\d+[km]?"
_TOKEN = re.compile(
    rf"""
      (?P<lev>(?:\d+(?:\.\d+)?)\s*x)(?![a-z0-9])        # 5x, 5 x, 2.5x
    | (?P<usd>\$\s*(?:{_NUM})|(?:{_NUM})(?:\$|\s*usdc?(?![a-z])))   # $200, 200$, 200 usd
                                        # (a trailing $ must touch the number: "99k $1000" is
                                        #  tp 99k + $1000, not $99k + 1000)
    | (?P<at>@)
    | (?P<num>{_NUM})(?![a-z0-9])
    | (?P<word>[a-z][a-z0-9]*(?:[-/][a-z0-9]+)?)
    | (?P<other>\S)
    """,
    re.VERBOSE | re.IGNORECASE,
)

SIDES = {"long": "LONG", "buy": "LONG", "short": "SHORT", "sell": "SHORT"}
FIELD_WORDS = {
    "sl": "sl", "stop": "sl", "stoploss": "sl", "stop-loss": "sl", "stop-lose": "sl",
    "tp": "tp", "target": "tp", "takeprofit": "tp", "take-profit": "tp",
    "entry": "entry", "at": "entry", "limit": "entry", "price": "entry",
    "size": "notional", "notional": "notional", "amount": "notional",
    "lev": "leverage", "leverage": "leverage",
}  # fmt: skip
MARKET_WORDS = {"market", "mkt", "now"}
FILLER = {"perp", "perps", "usd", "usdt", "usdc", "with", "and", "on", "the", "a", "an", "of", "x"}
_COIN_SUFFIX = re.compile(r"^([a-z0-9]{1,12}?)(?:-?perp|/?usdt?|/?usdc)$")
MAX_TEXT = 300


class DraftError(ValueError):
    """The text can't be turned into an order without guessing. `str(err)` is the reason."""


@dataclass(frozen=True, slots=True)
class OrderDraft:
    coin: str
    side: Side
    notional_usd: float
    leverage: float
    entry_px: float | None  # None = market
    sl_px: float | None
    tp_px: float | None
    defaults: tuple[str, ...] = field(default=())  # "notional" / "leverage" if not given

    def to_order(self) -> OpenOrder:
        return OpenOrder(
            coin=self.coin,
            side=self.side,
            notional_usd=self.notional_usd,
            leverage=self.leverage,
            entry_px=self.entry_px,
            sl_px=self.sl_px,
            tp_px=self.tp_px,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "coin": self.coin,
            "side": self.side,
            "notional_usd": self.notional_usd,
            "leverage": self.leverage,
            "entry_px": self.entry_px,
            "sl_px": self.sl_px,
            "tp_px": self.tp_px,
            "defaults": list(self.defaults),
        }

    def summary(self) -> str:
        entry = "market" if self.entry_px is None else f"@ {self.entry_px:,.6g}"
        parts = [f"{self.coin} {self.side} ${self.notional_usd:,.0f} {self.leverage:g}x {entry}"]
        parts.append(f"SL {self.sl_px:,.6g}" if self.sl_px is not None else "no SL")
        if self.tp_px is not None:
            parts.append(f"TP {self.tp_px:,.6g}")
        return "  ".join(parts)


def parse_number(raw: str) -> float:
    s = raw.strip().lower().replace("$", "").replace(",", "").replace("usdc", "").replace("usd", "")
    s = s.strip()
    mult = 1.0
    if s.endswith("k"):
        mult, s = 1_000.0, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000.0, s[:-1]
    value = float(s) * mult
    if value <= 0:
        raise DraftError(f"{raw!r} must be greater than 0")
    return value


def _coin_of(word: str) -> str:
    w = word.lower()
    m = _COIN_SUFFIX.match(w)
    return (m.group(1) if m else w).upper()


def parse_draft(
    text: str,
    *,
    default_notional: float = 100.0,
    default_leverage: float = 1.0,
    known_coins: Iterable[str] | None = None,
) -> OrderDraft:
    if not text or not text.strip():
        raise DraftError("empty message")
    if len(text) > MAX_TEXT:
        raise DraftError(f"too long for an order instruction (> {MAX_TEXT} characters)")
    known = {c.upper() for c in known_coins} if known_coins is not None else None

    values: dict[str, float | None] = {}
    coins: list[str] = []
    sides: set[str] = set()
    expecting: str | None = None  # field the next number belongs to
    market = False

    def put(name: str, value: float | None, raw: str) -> None:
        if name in values and values[name] != value:
            old = values[name]
            shown = "market" if old is None else f"{old:g}"
            raise DraftError(f"two different values for {name} ({shown} and {raw.strip()})")
        values[name] = value

    for m in _TOKEN.finditer(text):
        kind, raw = m.lastgroup, m.group(0)
        if kind == "lev":
            lev = float(raw.lower().replace("x", "").strip())
            if lev != int(lev) or lev < 1:
                raise DraftError(f"leverage must be a whole number >= 1, not {raw.strip()}")
            put("leverage", lev, raw)
            expecting = None if expecting == "leverage" else expecting
        elif kind == "usd":
            put("notional", parse_number(raw), raw)
            expecting = None if expecting == "notional" else expecting
        elif kind == "at":
            expecting = "entry"
        elif kind == "num":
            value = parse_number(raw)
            if expecting is not None:
                field_name, expecting = expecting, None
            elif "entry" not in values and not market and (coins or sides):
                field_name = "entry"  # first bare number after the coin/side is the entry
            else:
                raise DraftError(f"which price is {raw}? label it (sl / tp / @entry)")
            if field_name == "leverage" and (value != int(value) or value < 1):
                raise DraftError(f"leverage must be a whole number >= 1, not {raw}")
            put(field_name, value, raw)
        elif kind == "word":
            w = raw.lower()
            if w in SIDES:
                sides.add(SIDES[w])
            elif w in FIELD_WORDS:
                expecting = FIELD_WORDS[w]
            elif w in MARKET_WORDS:
                market = True
                put("entry", None, raw)
            elif w in FILLER:
                continue
            else:
                coin = _coin_of(w)
                if known is not None and coin not in known:
                    continue  # chatter ("lfg", "pls") is ignored once the coin list is known
                if coin not in coins:
                    coins.append(coin)
        # "other": punctuation like ":" "," "(" -- ignored

    if expecting is not None:
        raise DraftError(f"'{expecting}' has no number after it")
    if not coins:
        raise DraftError("no coin found (e.g. BTC, ETH, SOL)")
    if len(coins) > 1:
        raise DraftError(f"more than one coin ({', '.join(coins)})")
    if not sides:
        raise DraftError("say long or short (buy / sell)")
    if len(sides) > 1:
        raise DraftError("both long and short in one message")

    defaults: list[str] = []
    notional = values.get("notional")
    if notional is None:
        notional = default_notional
        defaults.append("notional")
    leverage = values.get("leverage")
    if leverage is None:
        leverage = default_leverage
        defaults.append("leverage")
    side: Side = "LONG" if sides == {"LONG"} else "SHORT"
    return OrderDraft(
        coin=coins[0],
        side=side,
        notional_usd=float(notional),
        leverage=float(leverage),
        entry_px=values.get("entry"),
        sl_px=values.get("sl"),
        tp_px=values.get("tp"),
        defaults=tuple(defaults),
    )
