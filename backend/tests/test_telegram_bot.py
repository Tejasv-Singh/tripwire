"""Telegram bot startup: background retries, flood-control waits, permanent token errors.
Uses a fake Application — no network."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import InvalidToken, NetworkError, RetryAfter, TimedOut

from tripwire.telegram_bot import RETRY_MAX_S, TelegramBot, retry_delay

# PTB's own RetryAfter.__init__ emits a v22.2 int->timedelta deprecation warning; not ours.
pytestmark = pytest.mark.filterwarnings("ignore::telegram.warnings.PTBDeprecationWarning")


class FakeUpdater:
    def __init__(self) -> None:
        self.running = False

    async def start_polling(self, **_kw: Any) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False


class FakeApp:
    def __init__(self, init_error: BaseException | None) -> None:
        self.init_error = init_error
        self.updater = FakeUpdater()
        self.running = False
        self.shutdowns = 0
        self.bot = SimpleNamespace(username="tripwire_test_bot")

    async def initialize(self) -> None:
        if self.init_error is not None:
            raise self.init_error

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def shutdown(self) -> None:
        self.shutdowns += 1


class Harness:
    """Factory that fails with the given errors in order, then succeeds."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.apps: list[FakeApp] = []
        self.sleeps: list[float] = []

    def factory(self, _token: str, _store: object) -> FakeApp:
        app = FakeApp(self.errors.pop(0) if self.errors else None)
        self.apps.append(app)
        return app

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def bot_for(h: Harness) -> TelegramBot:
    return TelegramBot("123:abc", store=None, app_factory=h.factory, sleep=h.sleep)


def test_retry_delay_backoff_and_flood_control() -> None:
    assert [retry_delay(TimedOut(), n) for n in (1, 2, 3)] == [5.0, 10.0, 20.0]
    assert retry_delay(NetworkError("x"), 50) == RETRY_MAX_S
    assert retry_delay(RetryAfter(32), 1) == 33.0  # server wait wins early on
    assert retry_delay(RetryAfter(3), 1) == 5.0  # ...but never below the backoff
    assert retry_delay(RetryAfter(timedelta(seconds=7)), 9) == RETRY_MAX_S  # backoff wins later


async def test_transient_errors_are_retried_until_running() -> None:
    h = Harness(TimedOut(), RetryAfter(32), NetworkError("reset"))
    bot = bot_for(h)
    await bot.start()
    assert bot.status == "starting"
    await bot.wait_started()
    assert bot.status == "running" and bot.detail == "@tripwire_test_bot"
    assert bot.attempts == 4
    assert h.sleeps == [5.0, 33.0, 20.0]
    assert all(a.shutdowns == 1 for a in h.apps[:3])  # failed attempts cleaned up
    assert h.apps[-1].updater.running
    await bot.stop()
    assert h.apps[-1].shutdowns == 1 and not h.apps[-1].updater.running


async def test_invalid_token_is_permanent() -> None:
    h = Harness(InvalidToken("Unauthorized"))
    bot = bot_for(h)
    await bot.start()
    await bot.wait_started()
    assert bot.status == "error" and "TELEGRAM_BOT_TOKEN" in bot.detail
    assert bot.attempts == 1 and h.sleeps == []


async def test_start_does_not_block_and_stop_cancels_retry_loop() -> None:
    gate = asyncio.Event()

    class SlowHarness(Harness):
        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            await gate.wait()  # never released: simulates a long backoff

    h = SlowHarness(TimedOut())
    bot = bot_for(h)
    await bot.start()  # returns immediately
    await asyncio.sleep(0.01)
    assert bot.status == "starting" and h.sleeps == [5.0]
    await bot.stop()  # must cancel the pending backoff cleanly
    assert bot._task is not None and bot._task.cancelled()


@pytest.mark.parametrize("token", ["", "   "])
async def test_empty_token_never_starts(token: str) -> None:
    h = Harness()
    bot = TelegramBot(token, store=None, app_factory=h.factory, sleep=h.sleep)
    await bot.start()
    assert bot.status == "disabled" and h.apps == []
    await bot.stop()


def test_polling_errors_log_one_line(caplog: pytest.LogCaptureFixture) -> None:
    from tripwire.telegram_bot import on_polling_error

    with caplog.at_level("WARNING", logger="tripwire.telegram_bot"):
        on_polling_error(NetworkError("httpx.ReadError: "))
    assert len(caplog.records) == 1 and caplog.records[0].exc_info is None
    assert "reconnecting automatically" in caplog.text
