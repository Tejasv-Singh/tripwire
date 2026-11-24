"""Per-client glasses UI flow: HUD -> menu(s) -> two-tap confirm -> sending -> result.

The BACKEND owns this state machine (the plugin only renders `ui` from each hud frame), so the
two-tap confirm cannot be skipped by a buggy or replayed client: the only way to produce an order
is `tap` on a menu item (arm) and then `tap` again inside the confirm window. Every armed order is
also pre-checked by the policy guard (`precheck`), and OrderService re-checks it when sending.

Intents: next | prev | tap | back   (plugin: swipe down/up, tap, double tap)

  hud      tap        -> "actions" menu for the selected position (refused if none / WATCH)
           next/prev  -> caller moves the position selection
  menu     next/prev  -> move the cursor (wraps)
           tap        -> the item (see MENUS); "Back" leaves the submenu / the menu
           back       -> hud
  confirm  tap        -> sending (caller submits the order), if >= MIN_CONFIRM_GAP_S after
                         arming and before CONFIRM_WINDOW_S ran out
           next/prev  -> back to the menu it was armed from (cancel)     back -> hud (cancel)
           timer      -> that menu, note "Confirm expired - not sent"
  sending  anything   -> ignored ("busy"); the caller calls finish() with the OrderResult
  result   anything   -> hud                 timer (RESULT_HOLD_S) -> hud

Menus (`kind`):
  actions  Close 25% | Close 50% | Close 100% | Move SL... | Back       (all reduce risk)
  sl       SL to breakeven | SL tighter 1% | Back                      (the current SL is fetched
                                                                         from the exchange when the
                                                                         submenu opens)
Stop-loss maths (StopOrder, validated again by policy.py):
  SL to breakeven  new trigger = entry price
  SL tighter 1%    new trigger = current SL moved 1 % of mark toward the price
                   (LONG: sl + 1% * mark, SHORT: sl - 1% * mark); needs an existing SL
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from tripwire.exchanges.base import StopInfo
from tripwire.hl.positions import Position
from tripwire.orders import CloseOrder, Order, OrderResult, ResultStatus, StopOrder
from tripwire.policy import Decision
from tripwire.trading import SLIPPAGE, round_px, round_size_down

# Policy pre-check at arm time (PolicyGuard.check bound to the live position / mark), so a
# blocked order is refused BEFORE the confirm screen. OrderService.submit() checks again.
Precheck = Callable[[Order], Decision]

Mode = Literal["hud", "menu", "confirm", "sending", "result"]
MenuKind = Literal["actions", "sl"]
Intent = Literal["next", "prev", "tap", "back"]

CONFIRM_WINDOW_S = 5.0  # second tap must land within this many seconds of the first
MIN_CONFIRM_GAP_S = 0.3  # ...and not sooner than this (a bounced/duplicated tap is not a confirm)
RESULT_HOLD_S = 8.0  # result screen returns to the HUD by itself after this long
SL_TIGHTEN_PCT = 1.0  # "SL tighter 1%": move the stop this % of mark toward the price


@dataclass(frozen=True, slots=True)
class MenuItem:
    key: str  # close25 | close50 | close100 | sl_menu | sl_breakeven | sl_tighten | back
    label: str


BACK = MenuItem("back", "Back")
MENUS: dict[str, tuple[MenuItem, ...]] = {
    "actions": (
        MenuItem("close25", "Close 25%"),
        MenuItem("close50", "Close 50%"),
        MenuItem("close100", "Close 100%"),
        MenuItem("sl_menu", "Move SL..."),
        BACK,
    ),
    "sl": (
        MenuItem("sl_breakeven", "SL to breakeven"),
        MenuItem("sl_tighten", f"SL tighter {SL_TIGHTEN_PCT:g}%"),
        BACK,
    ),
}
MENU = MENUS["actions"]  # Day-2 name
CLOSE_FRACTIONS = {"close25": 0.25, "close50": 0.5, "close100": 1.0}


@dataclass(frozen=True, slots=True)
class Armed:
    """The exact order the user is looking at on the confirm screen."""

    order: Order
    label: str
    view: dict[str, Any]  # what the confirm screen shows (wire fields, see docs/ws-protocol.md)
    armed_at: float  # loop.time()
    from_kind: MenuKind  # swipe-cancel returns here


@dataclass(slots=True)
class StopState:
    coin: str
    loaded: bool = False
    info: StopInfo | None = None


@dataclass(slots=True)
class Outcome:
    """What the caller (ws.py) must do after a call into the flow."""

    ok: bool = True
    note: str | None = None
    nav: Literal["next", "prev"] | None = None  # move the position selection (hud mode only)
    execute: Order | None = None  # submit this order now, then call finish()
    timer_s: float | None = None  # call on_timer(token) after this many seconds
    token: int = 0
    fetch_stop: str | None = None  # load this coin's current SL, then call set_stop()
    extra: dict[str, Any] = field(default_factory=dict)


class ActionFlow:
    def __init__(self) -> None:
        self.mode: Mode = "hud"
        self.kind: MenuKind = "actions"
        self.parent: MenuKind = "actions"  # where "Back" in a submenu goes
        self.cursor = 0
        self.coin: str | None = None  # every screen but the HUD is about this coin
        self.armed: Armed | None = None
        self.result: OrderResult | None = None
        self.note: str | None = None
        self.stop: StopState | None = None
        self.token = 0  # bumped on every transition; stale timers compare and do nothing

    # --- transitions -------------------------------------------------------------------------
    def _go(self, mode: Mode, *, note: str | None = None) -> int:
        self.mode, self.note = mode, note
        self.token += 1
        if mode == "hud":
            self.coin, self.armed, self.result, self.cursor = None, None, None, 0
            self.kind, self.parent, self.stop = "actions", "actions", None
        if mode == "menu":
            self.armed, self.result = None, None
        return self.token

    def _open_menu(self, kind: MenuKind, *, parent: MenuKind = "actions") -> Outcome:
        self.kind, self.parent, self.cursor = kind, parent, 0
        self._go("menu")
        if kind == "sl" and self.coin is not None:
            if self.stop is None or self.stop.coin != self.coin:
                self.stop = StopState(self.coin)
            if not self.stop.loaded:
                return Outcome(fetch_stop=self.coin)
        return Outcome()

    @property
    def items(self) -> tuple[MenuItem, ...]:
        return MENUS[self.kind]

    def set_stop(self, coin: str, info: StopInfo | None) -> bool:
        """The exchange answered "what is this coin's current SL?". True if the screen changed."""
        if self.stop is None or self.stop.coin != coin:
            return False
        self.stop = StopState(coin, True, info)
        return self.mode == "menu" and self.kind == "sl"

    def handle(
        self,
        intent: Intent,
        *,
        position: Position | None,
        now: float,
        trading: bool,
        size_decimals: int | None = None,
        watch: bool = False,
        precheck: Precheck | None = None,
    ) -> Outcome:
        """`position` = the position this client is looking at (the locked coin once in a menu).
        `watch` = the positions are someone else's (WATCH_ADDRESS): the menu stays closed."""
        if self.mode == "hud":
            if intent in ("next", "prev"):
                return Outcome(nav=intent)
            if intent == "tap":
                if watch:
                    return Outcome(ok=False, note="watch only")
                if position is None:
                    return Outcome(ok=False, note="no open position")
                self.coin = position.coin
                return self._open_menu("actions")
            return Outcome(note="already on the HUD")

        if self.mode == "menu":
            if intent in ("next", "prev"):
                self.cursor = (self.cursor + (1 if intent == "next" else -1)) % len(self.items)
                self.note = None
                return Outcome()
            if intent == "back":
                self._go("hud")
                return Outcome()
            item = self.items[self.cursor]
            ctx = _ArmCtx(position, now, trading, size_decimals, precheck)
            return self._select(item, ctx)

        if self.mode == "confirm":
            assert self.armed is not None
            if intent == "back":
                self._go("hud")
                return Outcome(note="cancelled")
            if intent in ("next", "prev"):
                self._back_to(self.armed.from_kind, note="Cancelled - not sent")
                return Outcome(note="cancelled")
            elapsed = now - self.armed.armed_at
            if elapsed < MIN_CONFIRM_GAP_S:
                return Outcome(ok=False, note="second tap too fast - tap again")
            if elapsed > CONFIRM_WINDOW_S:
                self._back_to(self.armed.from_kind, note="Confirm expired - not sent")
                return Outcome(ok=False, note="confirm expired")
            return self._send(position)

        if self.mode == "sending":
            return Outcome(ok=False, note="busy - order in flight")

        # result
        self._go("hud")
        return Outcome()

    def _back_to(self, kind: MenuKind, *, note: str) -> None:
        self.kind = kind
        self._go("menu", note=note)

    # --- menu items --------------------------------------------------------------------------
    def _select(self, item: MenuItem, ctx: _ArmCtx) -> Outcome:
        if item.key == "back":
            if self.kind == "sl":  # submenu -> parent menu
                self.kind, self.cursor = self.parent, 0
                self._go("menu")
                return Outcome()
            self._go("hud")
            return Outcome()
        if item.key == "sl_menu":
            return self._open_menu("sl", parent=self.kind)
        p = ctx.position
        if p is None or p.coin != self.coin:
            return self._fail("Position is closed - nothing to do")
        if not ctx.trading:
            return self._fail("Trading off: set HL_AGENT_KEY + HL_ACCOUNT_ADDRESS")
        if item.key in CLOSE_FRACTIONS:
            return self._arm_close(item, CLOSE_FRACTIONS[item.key], ctx)
        if item.key in ("sl_breakeven", "sl_tighten"):
            return self._arm_stop(item, ctx)
        return Outcome(ok=False, note=f"unknown item {item.key}")

    def _arm(self, order: Order, label: str, view: dict[str, Any], ctx: _ArmCtx) -> Outcome:
        if ctx.precheck is not None:
            decision = ctx.precheck(order)
            if not decision.allowed:
                return self._fail(f"Blocked: {decision.reason}", status="blocked")
        self.armed = Armed(order, label, view, ctx.now, self.kind)
        token = self._go("confirm")
        return Outcome(timer_s=CONFIRM_WINDOW_S, token=token)

    def _arm_close(self, item: MenuItem, fraction: float, ctx: _ArmCtx) -> Outcome:
        p = ctx.position
        assert p is not None
        exact = ctx.size_decimals is not None
        shown = p.size if fraction >= 1 else p.size * fraction
        if exact and fraction < 1:
            assert ctx.size_decimals is not None
            shown = round_size_down(shown, ctx.size_decimals)
        if shown <= 0:
            return self._fail("Size rounds to 0 - use a bigger %")
        order = CloseOrder(p.coin, p.side, fraction, None if fraction >= 1 else shown)
        view = {
            "side": p.side,
            "fraction": fraction,
            "size": shown,
            "all": fraction >= 1,
            "exact": exact or fraction >= 1,
            "est_px": p.mark,
            "slippage_pct": SLIPPAGE * 100,
        }
        return self._arm(order, item.label, view, ctx)

    def _arm_stop(self, item: MenuItem, ctx: _ArmCtx) -> Outcome:
        p = ctx.position
        assert p is not None
        if self.stop is None or not self.stop.loaded:
            return Outcome(ok=False, note="loading the current stop-loss - tap again")
        old = self.stop.info
        if item.key == "sl_breakeven":
            raw = p.entry
        else:
            if old is None:
                return self._fail("No stop-loss to tighten - use SL to breakeven")
            step = p.mark * SL_TIGHTEN_PCT / 100
            raw = old.trigger_px + step if p.side == "LONG" else old.trigger_px - step
        trigger = (
            round_px(raw, ctx.size_decimals)
            if ctx.size_decimals is not None
            else float(f"{raw:.5g}")
        )
        order = StopOrder(
            coin=p.coin,
            side=p.side,
            trigger_px=trigger,
            old_trigger_px=old.trigger_px if old else None,
            old_oid=old.oid if old else None,
            label=item.label,
        )
        view = {
            "side": p.side,
            "old_sl": old.trigger_px if old else None,
            "new_sl": trigger,
            "mark": p.mark,
            "entry": p.entry,
            "size": p.size,
        }
        return self._arm(order, item.label, view, ctx)

    def _send(self, position: Position | None) -> Outcome:
        armed = self.armed
        assert armed is not None
        order = armed.order
        reduces = order.kind in ("close", "stop")
        if reduces and (
            position is None or position.coin != order.coin or position.side != order.side
        ):
            return self._fail("Position changed - NOT sent")
        if isinstance(order, CloseOrder) and order.size is not None and position is not None:
            # never more than what was shown, capped at the live size
            order = CloseOrder(
                order.coin, order.side, order.fraction, min(order.size, position.size)
            )
        self._go("sending")
        return Outcome(execute=order)

    def _fail(self, message: str, status: ResultStatus = "rejected") -> Outcome:
        token = self._go("result")  # keeps self.armed (if any) so the result can name the action
        self.result = OrderResult(False, status, message)
        return Outcome(ok=False, note=message, timer_s=RESULT_HOLD_S, token=token)

    def finish(self, result: OrderResult) -> Outcome:
        """The order came back: show the result, then fall back to the HUD."""
        token = self._go("result")
        self.result = result
        return Outcome(ok=result.ok, note=result.message, timer_s=RESULT_HOLD_S, token=token)

    def on_timer(self, token: int) -> bool:
        """A timer set by an earlier Outcome fired. True if the screen changed."""
        if token != self.token:
            return False  # the user already moved on
        if self.mode == "confirm" and self.armed is not None:
            self._back_to(self.armed.from_kind, note="Confirm expired - not sent")
            return True
        if self.mode == "result":
            self._go("hud")
            return True
        return False

    # --- wire ----------------------------------------------------------------------------------
    def ui(self, now: float, *, trading: bool) -> dict[str, Any]:
        if self.mode == "hud":
            return {"mode": "hud"}
        base: dict[str, Any] = {"mode": self.mode, "coin": self.coin}
        if self.mode == "menu":
            base.update(
                kind=self.kind,
                items=[m.label for m in self.items],
                cursor=self.cursor,
                trading=trading,
                note=self.note,
            )
            if self.kind == "sl":
                loaded = self.stop is not None and self.stop.loaded
                info = self.stop.info if self.stop is not None else None
                base.update(sl_loaded=loaded, current_sl=info.trigger_px if info else None)
        elif self.mode in ("confirm", "sending") and self.armed is not None:
            a = self.armed
            base.update(
                action=a.order.kind,
                label=a.label,
                expires_in_ms=max(0, int((a.armed_at + CONFIRM_WINDOW_S - now) * 1000)),
                **a.view,
            )
        elif self.mode == "result" and self.result is not None:
            r = self.result
            base.update(
                ok=r.ok,
                status=r.status,
                message=r.message,
                label=self.armed.label if self.armed else None,
            )
        return base


@dataclass(frozen=True, slots=True)
class _ArmCtx:
    position: Position | None
    now: float
    trading: bool
    size_decimals: int | None
    precheck: Precheck | None
