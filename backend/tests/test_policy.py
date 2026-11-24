"""Policy guard: every rule, the kill switch, the loss lock, and OrderService enforcing it."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tripwire.db import Store
from tripwire.hl.positions import Position
from tripwire.orders import CloseOrder, OpenOrder, Order, OrderResult, OrderService, StopOrder
from tripwire.policy import (
    KillSwitch,
    PolicyContext,
    PolicyGuard,
    PolicyLimits,
    check_order,
)
from tripwire.telegram_bot import BotServices, make_kill_handlers

LIMITS = PolicyLimits(max_open_notional_usd=250, max_leverage=5, daily_loss_limit_usd=100)
LONG = Position(
    coin="ETH", side="LONG", size=0.5, entry=1700.0, mark=1750.0, upnl=25.0, upnl_pct=2.94,
    roe_pct=14.7, liq_px=1420.0, liq_dist_pct=18.9, leverage=5.0, mid=1750.0,
    mark_source="markPx", leverage_type="cross",
)  # fmt: skip
SHORT = dataclasses.replace(LONG, side="SHORT", liq_px=2000.0)


def ctx(position: Position | None = LONG, mark: float | None = 1750.0, **kw: Any) -> PolicyContext:
    return PolicyContext(position=position, mark=mark, **kw)


def open_order(**kw: Any) -> OpenOrder:
    base: dict[str, Any] = dict(
        coin="BTC", side="LONG", notional_usd=100.0, leverage=3.0, entry_px=96_500.0,
        sl_px=95_000.0, tp_px=99_000.0,
    )  # fmt: skip
    base.update(kw)
    return OpenOrder(**base)


def rule(order: Order, c: PolicyContext) -> str:
    return check_order(order, c, LIMITS).rule


# --- closes: always allowed while the position is there ----------------------------------------


def test_close_is_always_allowed_even_with_kill_switch_and_loss_lock() -> None:
    order = CloseOrder("ETH", "LONG", 0.5, 0.25)
    d = check_order(order, ctx(kill_switch=True, realized_pnl_today=-5000), LIMITS)
    assert d.allowed and d.rule == "reduce_only"


@pytest.mark.parametrize(
    ("order", "c", "expected"),
    [
        (CloseOrder("ETH", "LONG", 0.5, 0.25), ctx(position=None), "no_position"),
        (CloseOrder("ETH", "LONG", 0.5, 0.25), ctx(position=SHORT), "side_mismatch"),
        (CloseOrder("ETH", "LONG", 1.5, None), ctx(), "bad_size"),
        (CloseOrder("ETH", "LONG", 0.5, 0.0), ctx(), "bad_size"),
        (CloseOrder("ETH", "LONG", 0.5, 0.25), ctx(watch_only=True), "watch_only"),
    ],
)
def test_close_refusals(order: Order, c: PolicyContext, expected: str) -> None:
    assert rule(order, c) == expected


# --- stop-loss moves: only tighter, never past the mark ----------------------------------------


def stop(trigger: float, old: float | None = 1600.0, side: str = "LONG") -> StopOrder:
    return StopOrder("ETH", side, trigger, old, 11 if old else None, "SL test")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("order", "position", "expected", "allowed"),
    [
        (stop(1700.0), LONG, "sl_tighten", True),  # 1600 -> 1700 under mark 1750
        (stop(1700.0, old=None), LONG, "sl_new", True),  # no SL yet -> adding one is fine
        (stop(1550.0), LONG, "sl_loosen", False),  # further from price
        (stop(1600.0), LONG, "sl_loosen", False),  # same level is not tighter
        (stop(1760.0), LONG, "sl_would_trigger", False),  # above mark: would fire at once
        (stop(1750.0), LONG, "sl_would_trigger", False),  # at mark
        (stop(1800.0, old=1900.0, side="SHORT"), SHORT, "sl_tighten", True),
        (stop(1950.0, old=1900.0, side="SHORT"), SHORT, "sl_loosen", False),
        (stop(1700.0, old=1900.0, side="SHORT"), SHORT, "sl_would_trigger", False),
        (stop(1700.0), SHORT, "side_mismatch", False),
        (stop(1700.0), None, "no_position", False),
        (stop(0.0), LONG, "bad_price", False),
    ],
)
def test_stop_rules(
    order: StopOrder, position: Position | None, expected: str, allowed: bool
) -> None:
    d = check_order(order, ctx(position=position), LIMITS)
    assert (d.rule, d.allowed) == (expected, allowed)


def test_stop_needs_a_mark_and_ignores_kill_switch() -> None:
    assert rule(stop(1700.0), ctx(mark=None)) == "no_price"
    d = check_order(stop(1700.0), ctx(kill_switch=True, realized_pnl_today=-999), LIMITS)
    assert d.allowed


def test_stop_reasons_are_human() -> None:
    d = check_order(stop(1760.0), ctx(), LIMITS)
    assert d.reason == "SL 1,760 is above mark 1,750 - it would fire at once"
    d = check_order(stop(1550.0), ctx(), LIMITS)
    assert d.reason == "SL 1,550 is not tighter than 1,600 - loosening is refused"


# --- opening orders: SL, notional, leverage, loss lock, kill switch ----------------------------


def test_open_within_limits_is_allowed() -> None:
    d = check_order(open_order(), ctx(position=None, mark=96_400.0), LIMITS)
    assert d.allowed and d.rule == "open_within_limits"


@pytest.mark.parametrize(
    ("order", "c", "expected", "reason"),
    [
        (open_order(), ctx(position=None, kill_switch=True), "kill_switch",
         "Kill switch ON - only reduce-only orders"),
        (open_order(), ctx(position=None, realized_pnl_today=-100.0), "daily_loss_lock",
         "Daily loss lock: $-100 today (limit -$100)"),
        (open_order(sl_px=None), ctx(position=None), "sl_required",
         "Opening orders need a stop-loss (sl)"),
        (open_order(notional_usd=1000.0), ctx(position=None), "max_notional",
         "Notional $1,000 > max $250"),
        (open_order(leverage=10.0), ctx(position=None), "max_leverage", "Leverage 10x > max 5x"),
        (open_order(notional_usd=0.0), ctx(position=None), "bad_size",
         "Notional must be > 0 and leverage >= 1x"),
        (open_order(sl_px=97_000.0), ctx(position=None), "sl_wrong_side",
         "SL 97,000 must be below entry 96,500 for a long"),
        (open_order(side="SHORT", sl_px=95_000.0, tp_px=90_000.0), ctx(position=None),
         "sl_wrong_side", "SL 95,000 must be above entry 96,500 for a short"),
        (open_order(tp_px=95_500.0, sl_px=95_000.0), ctx(position=None), "tp_wrong_side",
         "TP 95,500 must be above entry 96,500 for a long"),
        (open_order(coin="ETH", entry_px=1750.0, sl_px=1800.0, tp_px=1600.0, side="SHORT"),
         ctx(position=LONG), "opposite_position",
         "You are LONG ETH - a SHORT would flip it; use Close"),
        (open_order(entry_px=None), ctx(position=None, mark=None), "no_price", "No price for BTC"),
        (open_order(), ctx(position=None, watch_only=True), "watch_only",
         "Watch only - this is not your account"),
    ],
)  # fmt: skip
def test_open_refusals(order: OpenOrder, c: PolicyContext, expected: str, reason: str) -> None:
    d = check_order(order, c, LIMITS)
    assert (d.allowed, d.rule, d.reason) == (False, expected, reason)


def test_rule_order_kill_switch_beats_everything_for_opens() -> None:
    bad = open_order(notional_usd=10_000.0, leverage=50.0, sl_px=None)
    assert rule(bad, ctx(position=None, kill_switch=True, realized_pnl_today=-1e6)) == "kill_switch"


def test_market_open_uses_mark_and_limits_are_inclusive() -> None:
    at_limit = open_order(entry_px=None, notional_usd=250.0, leverage=5.0)
    assert check_order(at_limit, ctx(position=None, mark=96_400.0), LIMITS).allowed
    loss_just_under = ctx(position=None, mark=96_400.0, realized_pnl_today=-99.99)
    assert check_order(at_limit, loss_just_under, LIMITS).allowed


def test_adding_to_the_same_side_is_allowed() -> None:
    add = open_order(coin="ETH", entry_px=None, sl_px=1650.0, tp_px=None)
    assert check_order(add, ctx(position=LONG), LIMITS).allowed


# --- kill switch persistence + guard wrapper ---------------------------------------------------


def test_kill_switch_env_and_telegram_flags(tmp_path: Path) -> None:
    store = Store(tmp_path / "k.sqlite3")
    store.init()
    ks = KillSwitch(False, store)
    ks.load()
    assert not ks.engaged and ks.source == "off"
    ks.engage()
    again = KillSwitch(False, store)
    again.load()
    assert again.engaged and again.source == "telegram"  # survives a restart
    assert again.release() is True and not again.engaged
    env = KillSwitch(True, store)
    env.load()
    assert env.engaged and env.source == "env" and env.release() is False and env.engaged


async def test_guard_refresh_keeps_last_value_on_error() -> None:
    values = iter([-150.0])

    async def pnl() -> float:
        return next(values)  # second call raises StopIteration

    guard = PolicyGuard(LIMITS, KillSwitch(False, None), pnl_source=pnl)
    await guard.refresh()
    assert guard.realized_pnl_today == -150.0 and guard.loss_locked
    await guard.refresh()
    assert guard.realized_pnl_today == -150.0
    assert guard.status()["loss_locked"] is True


async def test_order_service_blocks_before_the_exchange() -> None:
    sent: list[Order] = []

    async def send(order: Order) -> OrderResult:
        sent.append(order)
        return OrderResult(True, "filled", "ok")

    ks = KillSwitch(True, None)
    svc = OrderService(PolicyGuard(LIMITS, ks))
    r = await svc.submit(open_order(), position=None, mark=96_400.0, send=send)
    assert (r.ok, r.status, r.message) == (
        False,
        "blocked",
        "Kill switch ON - only reduce-only orders",
    )
    assert sent == []
    r = await svc.submit(
        CloseOrder("ETH", "LONG", 1.0, None), position=LONG, mark=1750.0, send=send
    )
    assert r.ok and len(sent) == 1  # a close still goes through with the kill switch on


# --- Telegram /kill and /unkill ----------------------------------------------------------------


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def update_from(user_id: int) -> Any:
    return SimpleNamespace(
        effective_message=FakeMessage(), effective_user=SimpleNamespace(id=user_id)
    )


async def test_kill_commands_are_owner_only(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.sqlite3")
    store.init()
    ks = KillSwitch(False, store)
    kill, unkill = make_kill_handlers(BotServices(owner_id=42, kill_switch=ks))
    stranger = update_from(7)
    await kill.callback(stranger, None)  # type: ignore[arg-type]
    assert stranger.effective_message.replies == ["Not authorized."] and not ks.engaged
    owner = update_from(42)
    await kill.callback(owner, None)  # type: ignore[arg-type]
    assert ks.engaged and owner.effective_message.replies[0].startswith("KILL SWITCH ON")
    await unkill.callback(owner, None)  # type: ignore[arg-type]
    assert not ks.engaged and owner.effective_message.replies[-1] == "Kill switch released."
    no_owner = make_kill_handlers(BotServices(owner_id=None, kill_switch=ks))[0]
    anyone = update_from(42)
    await no_owner.callback(anyone, None)  # type: ignore[arg-type]
    assert anyone.effective_message.replies == ["Not authorized."]  # no owner set: nobody
