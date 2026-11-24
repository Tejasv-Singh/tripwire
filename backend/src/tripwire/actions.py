"""Per-client glasses UI flow: HUD -> actions menu -> two-tap confirm -> sending -> result.

The BACKEND owns this state machine (the plugin only renders `ui` from each hud frame), so the
two-tap confirm cannot be skipped by a buggy or replayed client: the only way to produce an order
is `tap` on a menu item (arm) and then `tap` again inside the confirm window.

Intents: next | prev | tap | back   (plugin: swipe down/up, tap, double tap)

  hud      tap        -> menu for the selected position (refused if there is none)
           next/prev  -> caller moves the position selection
  menu     next/prev  -> move the cursor (wraps)
           tap        -> "Back": hud;  "Close N%": arm -> confirm  (or result error if trading off)
           back       -> hud
  confirm  tap        -> sending (caller executes the CloseRequest), if >= MIN_CONFIRM_GAP_S after
                         arming and before CONFIRM_WINDOW_S ran out
           next/prev  -> menu (cancel)       back -> hud (cancel)
           timer      -> menu, note "Confirm expired - not sent"
  sending  anything   -> ignored ("busy"); the caller calls finish() with the OrderResult
  result   anything   -> hud                 timer (RESULT_HOLD_S) -> hud

Every menu item is reduce-only (a close of 25 / 50 / 100 % of the position). There is no menu item
that opens, adds to or flips a position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tripwire.hl.positions import Position, Side
from tripwire.trading import SLIPPAGE, CloseRequest, OrderResult, round_size_down

Mode = Literal["hud", "menu", "confirm", "sending", "result"]
Intent = Literal["next", "prev", "tap", "back"]

CONFIRM_WINDOW_S = 5.0  # second tap must land within this many seconds of the first
MIN_CONFIRM_GAP_S = 0.3  # ...and not sooner than this (a bounced/duplicated tap is not a confirm)
RESULT_HOLD_S = 8.0  # result screen returns to the HUD by itself after this long


@dataclass(frozen=True, slots=True)
class MenuItem:
    label: str
    fraction: float | None  # None = "Back"


MENU: tuple[MenuItem, ...] = (
    MenuItem("Close 25%", 0.25),
    MenuItem("Close 50%", 0.5),
    MenuItem("Close 100%", 1.0),
    MenuItem("Back", None),
)


@dataclass(frozen=True, slots=True)
class Armed:
    """The exact order the user is looking at on the confirm screen."""

    coin: str
    side: Side
    label: str
    fraction: float
    shown_size: float  # what the confirm screen shows (rounded down to szDecimals when known)
    exact: bool  # shown_size is already rounded to the coin's szDecimals
    est_px: float  # mark price when armed (display only)
    armed_at: float  # loop.time()


@dataclass(slots=True)
class Outcome:
    """What the caller (ws.py) must do after a call into the flow."""

    ok: bool = True
    note: str | None = None
    nav: Literal["next", "prev"] | None = None  # move the position selection (hud mode only)
    execute: CloseRequest | None = None  # send this order now, then call finish()
    timer_s: float | None = None  # call on_timer(token) after this many seconds
    token: int = 0


class ActionFlow:
    def __init__(self) -> None:
        self.mode: Mode = "hud"
        self.cursor = 0
        self.coin: str | None = None  # menu/confirm/sending/result are about this coin
        self.armed: Armed | None = None
        self.result: OrderResult | None = None
        self.note: str | None = None
        self.token = 0  # bumped on every transition; stale timers compare and do nothing

    # --- transitions -------------------------------------------------------------------------
    def _go(self, mode: Mode, *, note: str | None = None) -> int:
        self.mode, self.note = mode, note
        self.token += 1
        if mode == "hud":
            self.coin, self.armed, self.result, self.cursor = None, None, None, 0
        if mode == "menu":
            self.armed, self.result = None, None
        return self.token

    def handle(
        self,
        intent: Intent,
        *,
        position: Position | None,
        now: float,
        trading: bool,
        size_decimals: int | None = None,
    ) -> Outcome:
        """`position` = the position this client is looking at (the locked coin once in a menu)."""
        if self.mode == "hud":
            if intent in ("next", "prev"):
                return Outcome(nav=intent)
            if intent == "tap":
                if position is None:
                    return Outcome(ok=False, note="no open position")
                self.coin = position.coin
                self._go("menu")
                return Outcome()
            return Outcome(note="already on the HUD")

        if self.mode == "menu":
            if intent in ("next", "prev"):
                self.cursor = (self.cursor + (1 if intent == "next" else -1)) % len(MENU)
                self.note = None
                return Outcome()
            if intent == "back":
                self._go("hud")
                return Outcome()
            item = MENU[self.cursor]
            if item.fraction is None:  # "Back"
                self._go("hud")
                return Outcome()
            return self._arm(item, position, now, trading, size_decimals)

        if self.mode == "confirm":
            assert self.armed is not None
            if intent == "back":
                self._go("hud")
                return Outcome(note="cancelled")
            if intent in ("next", "prev"):
                self._go("menu", note="Cancelled - not sent")
                return Outcome(note="cancelled")
            elapsed = now - self.armed.armed_at
            if elapsed < MIN_CONFIRM_GAP_S:
                return Outcome(ok=False, note="second tap too fast - tap again")
            if elapsed > CONFIRM_WINDOW_S:
                self._go("menu", note="Confirm expired - not sent")
                return Outcome(ok=False, note="confirm expired")
            return self._send(position)

        if self.mode == "sending":
            return Outcome(ok=False, note="busy - order in flight")

        # result
        self._go("hud")
        return Outcome()

    def _arm(
        self,
        item: MenuItem,
        position: Position | None,
        now: float,
        trading: bool,
        size_decimals: int | None,
    ) -> Outcome:
        assert item.fraction is not None
        if position is None or position.coin != self.coin:
            return self._fail("Position is closed - nothing to do")
        if not trading:
            return self._fail("Trading off: set HL_AGENT_KEY + HL_ACCOUNT_ADDRESS")
        raw = position.size * item.fraction
        exact = size_decimals is not None
        shown = position.size if item.fraction >= 1 else raw
        if exact and item.fraction < 1:
            assert size_decimals is not None
            shown = round_size_down(raw, size_decimals)
        if shown <= 0:
            return self._fail("Size rounds to 0 - use a bigger %")
        self.armed = Armed(
            coin=position.coin,
            side=position.side,
            label=item.label,
            fraction=item.fraction,
            shown_size=shown,
            exact=exact or item.fraction >= 1,
            est_px=position.mark,
            armed_at=now,
        )
        token = self._go("confirm")
        return Outcome(timer_s=CONFIRM_WINDOW_S, token=token)

    def _send(self, position: Position | None) -> Outcome:
        armed = self.armed
        assert armed is not None
        if position is None or position.coin != armed.coin or position.side != armed.side:
            return self._fail("Position changed - NOT sent")
        size: float | None = None  # 100 % -> close whatever the live size is
        if armed.fraction < 1:
            size = min(armed.shown_size, position.size)  # never more than what was shown
        self._go("sending")
        return Outcome(
            execute=CloseRequest(
                coin=armed.coin, side=armed.side, fraction=armed.fraction, size=size
            )
        )

    def _fail(self, message: str) -> Outcome:
        token = self._go("result")  # keeps self.armed (if any) so the result can name the action
        self.result = OrderResult(False, "rejected", message)
        return Outcome(ok=False, note=message, timer_s=RESULT_HOLD_S, token=token)

    def finish(self, result: OrderResult) -> Outcome:
        """The executor returned: show the result, then fall back to the HUD."""
        token = self._go("result")
        self.result = result
        return Outcome(ok=result.ok, note=result.message, timer_s=RESULT_HOLD_S, token=token)

    def on_timer(self, token: int) -> bool:
        """A timer set by an earlier Outcome fired. True if the screen changed."""
        if token != self.token:
            return False  # the user already moved on
        if self.mode == "confirm":
            self._go("menu", note="Confirm expired - not sent")
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
                items=[m.label for m in MENU], cursor=self.cursor, trading=trading, note=self.note
            )
        elif self.mode in ("confirm", "sending") and self.armed is not None:
            a = self.armed
            base.update(
                side=a.side,
                label=a.label,
                fraction=a.fraction,
                size=a.shown_size,
                all=a.fraction >= 1,
                exact=a.exact,
                est_px=a.est_px,
                slippage_pct=SLIPPAGE * 100,
                expires_in_ms=max(0, int((a.armed_at + CONFIRM_WINDOW_S - now) * 1000)),
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
