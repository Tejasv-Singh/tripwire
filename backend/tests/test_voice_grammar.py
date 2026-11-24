"""Voice grammar (Day 3, item 2): clean and NOISY Whisper transcripts -> command or reason."""

from __future__ import annotations

import pytest

from tripwire.voice_grammar import VoiceCommand, clean_transcript, parse_voice

CLOSE_25, CLOSE_50, CLOSE_ALL = (VoiceCommand("close", f) for f in (0.25, 0.5, 1.0))
BE, TIGHT = VoiceCommand("sl_breakeven"), VoiceCommand("sl_tighten")
SNOOZE, CANCEL, STATUS = VoiceCommand("snooze"), VoiceCommand("cancel"), VoiceCommand("status")


@pytest.mark.parametrize(
    ("transcript", "command"),
    [
        # clean
        ("close half", CLOSE_50), ("Close half.", CLOSE_50), ("close 50%", CLOSE_50),
        ("close fifty percent", CLOSE_50), ("close a quarter", CLOSE_25), ("Close 25.", CLOSE_25),
        ("close twenty five percent", CLOSE_25), ("close all", CLOSE_ALL),
        ("Close everything!", CLOSE_ALL), ("close 100%", CLOSE_ALL), ("flatten", CLOSE_ALL),
        ("close the whole position", CLOSE_ALL), ("trim half", CLOSE_50),
        ("stop to breakeven", BE), ("Move stop to break even.", BE), ("breakeven", BE),
        ("stop-loss to break-even", BE), ("stop tighter", TIGHT), ("Tighten the stop.", TIGHT),
        ("tighter", TIGHT), ("snooze", SNOOZE), ("Snooze it.", SNOOZE), ("cancel", CANCEL),
        ("Never mind.", CANCEL), ("abort", CANCEL), ("status", STATUS),
        ("What's my status?", STATUS),
        # noisy / misheard
        ("clothes half", CLOSE_50), ("Close have.", CLOSE_50), ("closed half", CLOSE_50),
        ("Clothes, 50 percent.", CLOSE_50), ("close haff", CLOSE_50),
        ("cloze everything", CLOSE_ALL), ("um, close half please", CLOSE_50),
        ("stop to brake even", BE), ("stop to breaking even", BE), ("stops to breakeven", BE),
        ("stop titer", TIGHT), ("snoozing", SNOOZE), ("snoose", SNOOZE),
        ("never mind, close half", CANCEL),  # cancel always wins
        ("close half... cancel", CANCEL),
        ("Status.", STATUS), ("stats", STATUS),
    ],
)  # fmt: skip
def test_understood(transcript: str, command: VoiceCommand) -> None:
    parsed = parse_voice(transcript)
    assert parsed.command == command, (transcript, parsed)
    assert parsed.error is None


@pytest.mark.parametrize(
    ("transcript", "error"),
    [
        ("", "didn't hear anything"),
        ("   ", "didn't hear anything"),
        ("Thank you.", "not a command"),  # Whisper's favourite hallucination on silence
        ("buy some bitcoin", "not a command"),  # opening a position is never a voice command
        ("close", "close how much? say quarter, half or all"),
        ("close 30 percent", "close 25, 50 or all (not other amounts)"),
        ("close half and all", "two different amounts"),
        ("close half and snooze", "one command at a time"),
        ("stop", "say 'stop to breakeven' or 'stop tighter'"),
        ("move my stop", "say 'stop to breakeven' or 'stop tighter'"),
        ("status and snooze", "one command at a time"),
    ],
)
def test_rejected_with_a_reason(transcript: str, error: str) -> None:
    parsed = parse_voice(transcript)
    assert parsed.command is None and parsed.error == error, (transcript, parsed)


def test_labels_and_cleaning() -> None:
    every = (CLOSE_25, CLOSE_50, CLOSE_ALL, BE, TIGHT, SNOOZE, CANCEL, STATUS)
    assert [c.label for c in every] == [
        "close 25%", "close 50%", "close all", "stop to breakeven", "stop tighter", "snooze",
        "cancel", "status",
    ]  # fmt: skip
    assert clean_transcript("  Close   half.  ") == "Close half"
    assert len(clean_transcript("x" * 200)) == 60
    assert parse_voice(" Close half. ").heard == "Close half"


def test_no_fuzzy_match_for_short_or_unrelated_words() -> None:
    # "all" must not turn into "close", "stock" must not turn into "stop" and arm something
    assert parse_voice("stock").command is None
    assert parse_voice("go long").command is None
