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
           (after a real fill, if a business chat wrote recently, the result screen is replaced
            by a reply offer as soon as the draft text is ready -- offer_reply())
  reply    tap        -> send the drafted reply (caller does it)    back -> skip
           next/prev  -> nothing                                     timer (REPLY_HOLD_S) -> hud

Session (screen 6):
  hud      back (double tap) -> end      "End session?" with the live numbers
  end      tap -> caller ends the session and calls show_summary()     next/prev -> hud
  summary  tap -> caller posts it to the journal channel (set_post_result)   next/prev -> hud
  (a double tap on end / summary exits the app -- the plugin does that itself)

Voice (voice()): a parsed command ARMS exactly what the matching menu tap would arm -- through
_select(), so the policy pre-check, WATCH and trading checks are identical -- and the two-tap
confirm still has to happen. "cancel" leaves any screen; on the reply screen any other speech
replaces the draft text. A stop-loss command opens the SL submenu, waits for the current stop
(fetch_stop), then arms (pending_voice).

Menus (`kind`):
  actions  Close 25% | Close 50% | Close 100% | Move SL... | Back       (all reduce risk)
  alert    Move SL | Close 50% | Close all | Snooze 5m                  (risk.py raised it; it
                                                                         waits if a confirm or an
                                                                         order is in progress)
  draft    Place order | Discard   (or only Discard if the policy guard blocks it)
           A forwarded Telegram message parsed by drafts.py: "FORWARDED - your instruction, not
           advice". Place order arms an OpenOrder: policy pre-check, then the two-tap confirm.
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

from tripwire.drafts import OrderDraft
from tripwire.exchanges.base import StopInfo
from tripwire.hl.positions import Position
from tripwire.inbox import ReplyTarget
from tripwire.orders import CloseOrder, OpenOrder, Order, OrderResult, ResultStatus, StopOrder
from tripwire.policy import Decision
from tripwire.risk import Alert
from tripwire.trading import SLIPPAGE, round_px, round_size_down
from tripwire.voice_grammar import VoiceCommand

# Policy pre-check at arm time (PolicyGuard.check bound to the live position / mark), so a
# blocked order is refused BEFORE the confirm screen. OrderService.submit() checks again.
Precheck = Callable[[Order], Decision]

Mode = Literal["hud", "menu", "confirm", "sending", "result", "reply", "end", "summary"]
MenuKind = Literal["actions", "sl", "alert", "draft"]
Intent = Literal["next", "prev", "tap", "back"]

CONFIRM_WINDOW_S = 5.0  # second tap must land within this many seconds of the first
MIN_CONFIRM_GAP_S = 0.3  # ...and not sooner than this (a bounced/duplicated tap is not a confirm)
RESULT_HOLD_S = 8.0  # result screen returns to the HUD by itself after this long
REPLY_HOLD_S = 20.0  # an unanswered reply offer is skipped after this long
SL_TIGHTEN_PCT = 1.0  # "SL tighter 1%": move the stop this % of mark toward the price
WATCH_ONLY = "Watch only - not your account, nothing sent"
VOICE_CLOSE_KEYS = {0.25: "close25", 0.5: "close50", 1.0: "close100"}


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
    "alert": (
        MenuItem("sl_menu", "Move SL"),
        MenuItem("close50", "Close 50%"),
        MenuItem("close100", "Close all"),
        MenuItem("snooze", "Snooze 5m"),
    ),
}
MENU = MENUS["actions"]  # Day-2 name
DRAFT_OK = (MenuItem("place", "Place order"), MenuItem("discard", "Discard"))
DRAFT_BLOCKED = (MenuItem("discard", "Discard"),)
CLOSE_FRACTIONS = {"close25": 0.25, "close50": 0.5, "close100": 1.0}


@dataclass(frozen=True, slots=True)
class Armed:
    """The exact order the user is looking at on the confirm screen."""

    order: Order
    label: str
    view: dict[str, Any]  # what the confirm screen shows (wire fields, see docs/ws-protocol.md)
    armed_at: float  # loop.time()
    from_kind: MenuKind  # swipe-cancel returns here


@dataclass(frozen=True, slots=True)
class ReplyOffer:
    target: ReplyTarget
    text: str
    source: str  # "openrouter" | "template"


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
    snooze: tuple[str, str] | None = None  # (coin, side): snooze this alert in the risk engine
    send_reply: ReplyOffer | None = None  # the user tapped "send" on a reply offer
    show_status: bool = False  # voice "status": the caller shows a one-line status
    blocked: bool = False  # the policy guard (or WATCH) refused something: count it
    end_session: bool = False  # "End session?" confirmed: caller ends it, then show_summary()
    post_summary: bool = False  # summary screen tapped: caller posts it to the journal
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
        self.alert: Alert | None = None  # the alert on screen (kind "alert" and its submenus)
        self.pending_alert: Alert | None = None  # raised during a confirm/order: shown after
        self.draft: OrderDraft | None = None  # the forwarded draft on screen (kind "draft")
        self.draft_decision: Decision | None = None  # the policy guard's verdict on it
        self.pending_draft: tuple[OrderDraft, Decision] | None = None
        self.reply: ReplyOffer | None = None  # mode "reply"
        self.pending_voice: str | None = None  # SL item key to arm once the stop has loaded
        self.summary: dict[str, Any] | None = None  # the ended session (mode "summary")
        self.summary_text = ""  # what a tap posts to the journal
        self.posted: bool | None = None  # None = not tried, True / False = journal result
        self.token = 0  # bumped on every transition; stale timers compare and do nothing

    def reset(self) -> None:
        """Back to a plain HUD with nothing pending (npm run demo:reset). The token keeps
        counting, so timers from before the reset stay stale."""
        token = self.token
        self.__init__()  # type: ignore[misc]
        self.token = token + 1

    # --- transitions -------------------------------------------------------------------------
    def _go(self, mode: Mode, *, note: str | None = None) -> int:
        if mode == "hud" and self.pending_alert is not None:
            alert, self.pending_alert = self.pending_alert, None
            self._show_alert(alert)  # an alert that waited for the order wins over the HUD
            return self.token
        if mode == "hud" and self.pending_draft is not None:
            pending, self.pending_draft = self.pending_draft, None
            self._show_draft(*pending)
            return self.token
        self.mode, self.note = mode, note
        self.token += 1
        if mode == "hud":
            self.coin, self.armed, self.result, self.cursor = None, None, None, 0
            self.kind, self.parent, self.stop, self.alert = "actions", "actions", None, None
            self.draft, self.draft_decision, self.reply = None, None, None
            self.pending_voice = None
            self.summary, self.summary_text, self.posted = None, "", None
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

    def raise_alert(self, alert: Alert) -> bool:
        """Show the RISK screen. True if it is on screen now; during a confirm or an order in
        flight it waits (False) and appears as soon as that screen is left."""
        if self.mode in ("confirm", "sending"):
            self.pending_alert = alert
            return False
        self._show_alert(alert)
        return True

    def _show_alert(self, alert: Alert) -> None:
        if self.stop is not None and self.stop.coin != alert.coin:
            self.stop = None
        self.coin, self.alert = alert.coin, alert
        self.kind, self.parent, self.cursor = "alert", "alert", 0
        self.mode, self.note = "menu", None
        self.armed, self.result = None, None
        self.token += 1

    def raise_draft(self, draft: OrderDraft, decision: Decision) -> bool:
        """Show a forwarded order draft. Waits (False) during a confirm / order in flight."""
        if self.mode in ("confirm", "sending"):
            self.pending_draft = (draft, decision)
            return False
        self._show_draft(draft, decision)
        return True

    def _show_draft(self, draft: OrderDraft, decision: Decision) -> None:
        self.coin, self.draft, self.draft_decision = draft.coin, draft, decision
        self.kind, self.parent, self.cursor = "draft", "draft", 0
        self.mode, self.note = "menu", None
        self.armed, self.result, self.alert = None, None, None
        self.token += 1

    @property
    def items(self) -> tuple[MenuItem, ...]:
        if self.kind == "draft":
            ok = self.draft_decision is not None and self.draft_decision.allowed
            return DRAFT_OK if ok else DRAFT_BLOCKED
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
        ref_px: float | None = None,
    ) -> Outcome:
        """`position` = the position this client is looking at (the locked coin once in a menu).
        `ref_px` = current price of the locked coin (market drafts estimate their size with it).
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
            self._go("end")  # back = double tap on the HUD
            return Outcome(note="end session? tap to end, swipe to keep going")

        if self.mode == "end":
            if intent == "tap":
                return Outcome(end_session=True)
            self._go("hud")
            return Outcome(note="session continues")

        if self.mode == "summary":
            if intent == "tap":
                if self.posted:
                    return Outcome(note="already posted")
                return Outcome(post_summary=True, note="posting to the journal")
            self._go("hud")
            return Outcome()

        if self.mode == "menu":
            if intent in ("next", "prev"):
                self.cursor = (self.cursor + (1 if intent == "next" else -1)) % len(self.items)
                self.note = None
                return Outcome()
            if intent == "back":
                self._go("hud")
                return Outcome()
            item = self.items[self.cursor]
            ctx = _ArmCtx(position, now, trading, size_decimals, precheck, watch, ref_px)
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

        if self.mode == "reply":
            offer = self.reply
            if intent == "tap" and offer is not None:
                self._go("hud")
                return Outcome(send_reply=offer, note=f"sending reply to {offer.target.name}")
            if intent == "back":
                self._go("hud")
                return Outcome(note="reply skipped")
            return Outcome(note="tap: send  2x: skip")

        # result
        self._go("hud")
        return Outcome()

    def show_summary(self, summary: dict[str, Any], text: str) -> None:
        """The session just ended: show its summary (a new session is already running)."""
        self._go("summary")
        self.summary, self.summary_text, self.posted = summary, text, None

    def set_post_result(self, ok: bool, note: str) -> bool:
        if self.mode != "summary":
            return False
        self.posted, self.note = ok, note
        return True

    def offer_reply(self, offer: ReplyOffer, result_token: int) -> int | None:
        """Replace the result screen with a reply offer -- only if the user is still looking at
        the result this offer belongs to. Returns the new token (for the REPLY_HOLD_S timer)."""
        if self.mode != "result" or self.token != result_token:
            return None
        self.reply = offer
        return self._go("reply")

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
        if item.key == "discard":
            self._go("hud")
            return Outcome(note="draft discarded")
        if item.key == "place":
            return self._arm_open(ctx)
        if item.key == "snooze":
            alert = self.alert
            self._go("hud")
            if alert is None:
                return Outcome(ok=False, note="no alert to snooze")
            return Outcome(snooze=(alert.coin, alert.side), note="alert snoozed 5 min")
        p = ctx.position
        if p is None or p.coin != self.coin:
            return self._fail("Position is closed - nothing to do")
        if ctx.watch:
            return self._fail(WATCH_ONLY, status="blocked")
        if not ctx.trading:
            return self._fail("Trading off: set HL_AGENT_KEY + HL_ACCOUNT_ADDRESS")
        if item.key in CLOSE_FRACTIONS:
            return self._arm_close(item, CLOSE_FRACTIONS[item.key], ctx)
        if item.key in ("sl_breakeven", "sl_tighten"):
            return self._arm_stop(item, ctx)
        return Outcome(ok=False, note=f"unknown item {item.key}")

    # --- voice ------------------------------------------------------------------------------
    @staticmethod
    def _item(key: str) -> MenuItem:
        for items in MENUS.values():
            for item in items:
                if item.key == key:
                    return item
        raise KeyError(key)

    def voice(
        self,
        command: VoiceCommand | None,
        heard: str,
        *,
        position: Position | None,
        now: float,
        trading: bool,
        size_decimals: int | None = None,
        watch: bool = False,
        precheck: Precheck | None = None,
        ref_px: float | None = None,
    ) -> Outcome:
        """Apply a spoken command (see voice_grammar.py). Never sends anything by itself."""
        ctx = _ArmCtx(position, now, trading, size_decimals, precheck, watch, ref_px)
        if self.mode == "reply" and self.reply is not None:
            if command is not None and command.kind == "cancel":
                self._go("hud")
                return Outcome(note="reply skipped")
            text = " ".join(heard.split())[:60].strip()
            if not text:
                return Outcome(ok=False, note="didn't hear a reply")
            self.reply = ReplyOffer(self.reply.target, text, "voice")
            return Outcome(note="reply text replaced - tap to send")
        if command is None:
            return Outcome(ok=False, note="not a command")
        if command.kind == "cancel":
            if self.mode == "hud":
                return Outcome(note="nothing to cancel")
            self._go("hud")
            return Outcome(note="cancelled")
        if self.mode in ("confirm", "sending"):
            return Outcome(ok=False, note="finish or cancel the current order first")
        if command.kind == "status":
            return Outcome(show_status=True)
        if command.kind == "snooze":
            if self.mode == "menu" and self.alert is not None:
                return self._select(self._item("snooze"), ctx)
            return Outcome(ok=False, note="no alert to snooze")
        if position is None:
            return Outcome(ok=False, note="no open position")
        if self.mode in ("hud", "result"):
            self.coin, self.kind = position.coin, "actions"
        if command.kind == "close":
            return self._select(self._item(VOICE_CLOSE_KEYS[command.fraction or 1.0]), ctx)
        # stop moves need the position's current stop: open the SL submenu, then arm
        if not (self.mode == "menu" and self.kind == "sl"):
            self._open_menu("sl", parent=self.kind if self.kind != "sl" else "actions")
        if self.stop is not None and self.stop.loaded:
            return self._select(self._item(command.kind), ctx)
        self.pending_voice = command.kind
        return Outcome(fetch_stop=self.coin, note="loading the current stop-loss")

    def take_pending_voice(self) -> str | None:
        key, self.pending_voice = self.pending_voice, None
        return key if self.mode == "menu" and self.kind == "sl" else None

    def select_key(
        self,
        key: str,
        *,
        position: Position | None,
        now: float,
        trading: bool,
        size_decimals: int | None = None,
        watch: bool = False,
        precheck: Precheck | None = None,
        ref_px: float | None = None,
    ) -> Outcome:
        """Arm a menu item by key (a voice command whose stop-loss lookup just finished)."""
        ctx = _ArmCtx(position, now, trading, size_decimals, precheck, watch, ref_px)
        return self._select(self._item(key), ctx)

    def _arm(self, order: Order, label: str, view: dict[str, Any], ctx: _ArmCtx) -> Outcome:
        if ctx.precheck is not None:
            decision = ctx.precheck(order)
            if not decision.allowed:
                return self._fail(f"Blocked: {decision.reason}", status="blocked")
        self.armed = Armed(order, label, view, ctx.now, self.kind)
        token = self._go("confirm")
        return Outcome(timer_s=CONFIRM_WINDOW_S, token=token)

    def _arm_open(self, ctx: _ArmCtx) -> Outcome:
        draft = self.draft
        if draft is None:
            return self._fail("No draft to place")
        if ctx.watch:
            return self._fail(WATCH_ONLY, status="blocked")
        if not ctx.trading:
            return self._fail("Trading off: set HL_AGENT_KEY + HL_ACCOUNT_ADDRESS (or DRY_RUN)")
        order: OpenOrder = draft.to_order()
        ref = draft.entry_px or ctx.ref_px
        view = {
            "side": draft.side,
            "notional": draft.notional_usd,
            "leverage": draft.leverage,
            "entry": draft.entry_px,
            "sl": draft.sl_px,
            "tp": draft.tp_px,
            "size": order.notional_usd / ref if ref else None,
            "exact": False,  # rounded to the lot size by the exchange adapter when sent
        }
        return self._arm(order, "Place order", view, ctx)

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
        via_alert = armed.from_kind == "alert" or (
            armed.from_kind == "sl" and self.parent == "alert"
        )
        self._go("sending")
        return Outcome(execute=order, extra={"via_alert": via_alert})

    def _fail(self, message: str, status: ResultStatus = "rejected") -> Outcome:
        token = self._go("result")  # keeps self.armed (if any) so the result can name the action
        self.result = OrderResult(False, status, message)
        return Outcome(
            ok=False, note=message, timer_s=RESULT_HOLD_S, token=token, blocked=status == "blocked"
        )

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
        if self.mode in ("result", "reply"):
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
            if self.kind == "alert" and self.alert is not None:
                base.update(alert=self.alert.to_dict())
            if self.kind == "draft" and self.draft is not None:
                d = self.draft_decision
                base.update(
                    draft=self.draft.to_dict(),
                    policy={"allowed": bool(d and d.allowed), "rule": d.rule if d else "",
                            "reason": d.reason if d else ""},
                )  # fmt: skip
        elif self.mode in ("confirm", "sending") and self.armed is not None:
            a = self.armed
            base.update(
                action=a.order.kind,
                label=a.label,
                expires_in_ms=max(0, int((a.armed_at + CONFIRM_WINDOW_S - now) * 1000)),
                **a.view,
            )
        elif self.mode == "summary" and self.summary is not None:
            base.update(summary=self.summary, posted=self.posted, note=self.note)
        elif self.mode == "reply" and self.reply is not None:
            base.update(to=self.reply.target.name, text=self.reply.text, source=self.reply.source)
            if self.result is not None:  # keep the fill visible: a template draft is instant
                base.update(result_status=self.result.status, result_message=self.result.message)
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
    watch: bool = False
    ref_px: float | None = None
