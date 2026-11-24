"""Constrained voice grammar: a Whisper transcript -> one of a handful of commands, or a reason.

Commands (examples of what is understood -- see tests/test_voice_grammar.py for many more):
  close 25 / 50 / all    "close half", "close a quarter", "close everything", "clothes 50 percent",
                         "trim half", "flatten" (= close all)
  stop to breakeven      "stop to breakeven", "move stop to break even", "breakeven"
  stop tighter           "stop tighter", "tighten the stop", "tighter"
  snooze                 "snooze", "snooze it"
  cancel                 "cancel", "never mind", "abort"   -- wins over everything else
  status                 "status", "what's my status"

Rules:
  * Words are normalised (case, punctuation, number words, "break even" -> breakeven), then
    fuzzy-matched against a small vocabulary (difflib, cutoff 0.75) plus an alias table of common
    mishearings ("clothes" -> close, "have" after close -> half).
  * "cancel" always wins: a phrase containing it never arms anything.
  * Exactly one intent, or the phrase is rejected ("one command at a time").
  * close needs an amount and it must be 25 / 50 / all ("close 30" is refused, not rounded).
  * Voice never sends: every command only ARMS what a menu tap would arm; the two-tap confirm
    still has to happen on the glasses.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["close", "sl_breakeven", "sl_tighten", "snooze", "cancel", "status"]

VOCAB = (
    "close", "stop", "breakeven", "tighter", "snooze", "cancel", "status", "half", "quarter",
    "all", "everything", "percent", "flatten", "trim", "abort",
)  # fmt: skip
ALIASES = {
    "clothes": "close", "closed": "close", "closes": "close", "cloze": "close", "clothe": "close",
    "trimmed": "trim", "reduce": "trim", "cut": "trim",
    "halve": "half", "halves": "half", "haff": "half",
    "quarters": "quarter", "quota": "quarter",
    "entire": "all", "whole": "all", "full": "all", "every": "all",
    "tighten": "tighter", "tight": "tighter", "titer": "tighter", "tide": "tighter",
    "snoozed": "snooze", "snoozing": "snooze", "snoose": "snooze",
    "stops": "stop", "stoploss": "stop", "sl": "stop",
    "nevermind": "cancel", "canceled": "cancel", "cancelled": "cancel",
    "statuses": "status", "stats": "status",
}  # fmt: skip
NUMBER_WORDS = {
    "twenty five": "25", "twenty-five": "25", "fifty": "50", "one hundred": "100",
    "a hundred": "100", "hundred": "100",
}  # fmt: skip
PHRASES = {
    r"\bbrakes? even\b": "breakeven", r"\bbreak(?:s|ing)? even\b": "breakeven",
    r"\bbreak-even\b": "breakeven", r"\bstop[- ]loss\b": "stop", r"\bnever mind\b": "cancel",
    r"\bclose\s+have\b": "close half", r"\bclothes\s+have\b": "close half",
}  # fmt: skip
STOPWORDS = {
    "a", "an", "the", "to", "my", "it", "of", "please", "move", "set", "put", "at", "up", "now",
    "and", "this", "that", "position", "whats", "what", "is", "show", "me", "go",
}  # fmt: skip
AMOUNT_WORDS = {"half": 0.5, "quarter": 0.25, "all": 1.0, "everything": 1.0}
NUMBER_AMOUNTS = {25: 0.25, 50: 0.5, 100: 1.0}
FUZZY_CUTOFF = 0.75


@dataclass(frozen=True, slots=True)
class VoiceCommand:
    kind: Kind
    fraction: float | None = None  # close only: 0.25 / 0.5 / 1.0

    @property
    def label(self) -> str:
        if self.kind == "close":
            return {0.25: "close 25%", 0.5: "close 50%", 1.0: "close all"}[self.fraction or 1.0]
        return {
            "sl_breakeven": "stop to breakeven",
            "sl_tighten": "stop tighter",
            "snooze": "snooze",
            "cancel": "cancel",
            "status": "status",
        }[self.kind]


@dataclass(frozen=True, slots=True)
class VoiceParse:
    heard: str  # the cleaned transcript, as shown on the glasses
    command: VoiceCommand | None = None
    error: str | None = None  # why nothing was understood (shown instead of a command)


def clean_transcript(text: str) -> str:
    """What the glasses show: single spaces, no trailing period, at most 60 characters."""
    t = " ".join(text.split()).strip().rstrip(".!?").strip()
    return t[:60]


def tokens(text: str) -> list[str]:
    t = text.lower().replace("%", " percent ").replace("'", "")
    for words, digits in NUMBER_WORDS.items():
        t = re.sub(rf"\b{re.escape(words)}\b", digits, t)
    for pattern, repl in PHRASES.items():
        t = re.sub(pattern, repl, t)
    out: list[str] = []
    for raw in re.findall(r"[a-z]+|\d+", t):
        if raw.isdigit():
            out.append(raw)
            continue
        word = ALIASES.get(raw, raw)
        if word not in VOCAB and word not in STOPWORDS and len(word) >= 4:
            match = difflib.get_close_matches(word, VOCAB, n=1, cutoff=FUZZY_CUTOFF)
            word = match[0] if match else word
        if word not in STOPWORDS:
            out.append(word)
    return out


def parse_voice(transcript: str) -> VoiceParse:
    heard = clean_transcript(transcript)
    if not heard:
        return VoiceParse(heard, error="didn't hear anything")
    words = tokens(heard)
    w = set(words)
    if "cancel" in w or "abort" in w:
        return VoiceParse(heard, VoiceCommand("cancel"))

    intents: list[VoiceCommand | str] = []  # str = an intent we recognised but can't complete
    if "status" in w:
        intents.append(VoiceCommand("status"))
    if "snooze" in w:
        intents.append(VoiceCommand("snooze"))
    if "breakeven" in w:
        intents.append(VoiceCommand("sl_breakeven"))
    elif "tighter" in w:
        intents.append(VoiceCommand("sl_tighten"))
    elif "stop" in w:
        intents.append("say 'stop to breakeven' or 'stop tighter'")
    if "flatten" in w:
        intents.append(VoiceCommand("close", 1.0))
    elif "close" in w or "trim" in w:
        intents.append(_close_amount(words))

    if not intents:
        return VoiceParse(heard, error="not a command")
    if len(intents) > 1:
        return VoiceParse(heard, error="one command at a time")
    only = intents[0]
    if isinstance(only, str):
        return VoiceParse(heard, error=only)
    return VoiceParse(heard, only)


def _close_amount(words: list[str]) -> VoiceCommand | str:
    fractions: set[float] = {AMOUNT_WORDS[x] for x in words if x in AMOUNT_WORDS}
    for x in words:
        if x.isdigit():
            n = int(x)
            if n not in NUMBER_AMOUNTS:
                return "close 25, 50 or all (not other amounts)"
            fractions.add(NUMBER_AMOUNTS[n])
    if not fractions:
        return "close how much? say quarter, half or all"
    if len(fractions) > 1:
        return "two different amounts"
    return VoiceCommand("close", fractions.pop())
