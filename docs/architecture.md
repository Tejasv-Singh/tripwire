# Architecture

Tripwire is a trading-session risk co-pilot: live Hyperliquid **testnet** positions on Even
Realities G2 glasses, navigated with ring/temple taps, plus a Telegram Business bot for messages
and alerts.
- Day 1: read-only HUD + bot skeleton.
- Day 2: ROE, an actions menu on tap, and **reduce-only closes (25 / 50 / 100 %) behind a
  two-tap confirm**.
- Day 2 continued:
  - WATCH mode; a **policy guard** on one order path; an **exchange adapter** interface;
    **DRY_RUN** signing.
  - Stop-loss moves; **liquidation alerts** (glasses or Telegram DM).
  - Forward-to-bot **order drafts**; the Business inbox line, reply drafts, `/status`.

## Components

```
 ┌─────────────────────────── phone ───────────────────────────┐
 │ Even App (WebView)                                           │      ┌──────────── G2 glasses ───────────┐
 │  plugin/  (Vite + TS, even_hub_sdk 0.0.16)                   │ BLE  │ 576×288 text container            │
 │   main.ts ─ glasses.ts ── createStartUpPageContainer (once) ─┼─────►│ "TRIPWIRE   TESTNET   HH:MM ..."  │
 │            └──────────── textContainerUpgrade (live text) ───┼─────►│                                   │
 │   input.ts ◄── onEvenHubEvent (swipe / tap / lifecycle) ◄────┼──────┤ ring / temple touch               │
 │   link.ts  ── wss://<host>/ws?code=… ──┐                     │      └───────────────────────────────────┘
 │   api.ts   ── https://<host>/healthz ──┤ (ngrok-skip header) │
 │   voice.ts ── hold = push-to-talk: mic PCM → binary WS frames│
 └────────────────────────────────────────┼─────────────────────┘
                                          │  HTTPS / WSS
                          ┌───────────────▼────────────────┐
                          │ ngrok  caucus-strategic-endorse │
                          │        .ngrok-free.dev → :8000  │
                          └───────────────┬────────────────┘
 ┌─────────────────────────── laptop ─────┼───────────────────────────────────────────────┐
 │ backend/  FastAPI + uvicorn (Python 3.12, one asyncio event loop)                       │
 │   main.py   create_app(): testnet guard → state → feed task → bot → routes              │
 │   ws.py     /ws: pairing, frames ≤ 2/s + 1 s heartbeat, intents, voice, acks            │
 │   actions.py  per-client screens: hud → menu(actions|sl|alert|draft) → confirm →         │
 │               sending → result → reply → end → summary (screen 6)                       │
 │   orders.py   Close/Stop/OpenOrder + OrderService.submit  ◄── THE only order path       │
 │   policy.py   PolicyGuard: reduce-only free; opens need SL, ≤$250, ≤5x, loss lock, kill  │
 │   exchanges/  ExchangeAdapter: hyperliquid.py (sign → POST /exchange or DRY_RUN log),    │
 │               fake.py (in-memory), bybit_demo.py (stub)                                  │
 │   risk.py     RiskEngine (threshold, hysteresis, snooze) + AlertService (glasses / DM)   │
 │   drafts.py   forwarded text → OrderDraft (regex, no LLM)                               │
 │   inbox.py    business messages → HUD line (30 chars, memory only)                       │
 │   replies.py  ≤60-char reply drafts: OpenRouter (facts only) → template fallback         │
 │   trading.py  pure order helpers (rounding, reply parsing)                              │
 │   voice.py    push-to-talk PCM → faster-whisper base, CPU int8, local only              │
 │   voice_grammar.py  transcript → close 25/50/all, stop, snooze, cancel, status          │
 │   session.py  SessionTracker: trades, realized/session P&L, drawdown → journal          │
 │   latency.py  exchange event (src_ms) → render ack: p50 / p95 in /status                │
 │   golive.py   npm run golive:check: keys, agent, funds, guard, $15 payload              │
 │   hud.py    MarketState + ui → JSON frame                                               │
 │   state.py  MarketState (mids, marks, positions, listeners; own / WATCH / price mode)   │
 │   hl/feed.py      ── wss://api.hyperliquid-testnet.xyz/ws ──► Hyperliquid TESTNET       │
 │   hl/positions.py pure math: uPnL, uPnL %, ROE, liq distance                            │
 │   telegram_bot.py ── long polling ──► api.telegram.org (Bot API): /start /status /kill  │
 │                   /unkill /threshold /summary, drafts, inbox, DMs, journal              │
 │   db.py     SQLite: business conns, intent/order logs, sessions, kv (no text)           │
 └─────────────────────────────────────────────────────────────────────────────────────────┘
```

## The one order path

```
glasses tap (arm) ── PolicyGuard.check (precheck: block reason instead of a confirm)
glasses tap (confirm, 0.3–5 s later)
        │
        ▼
OrderService.submit(order)  ── guard.refresh() (realized PnL today) ── guard.check() ── blocked? → result "BLOCKED"
        │
        ▼
adapter.close_reduce_only / set_sl / place_order   (exchanges/, chosen by order.kind in ws.send_order)
        │
        ▼
hyperliquid.py: build wire (SDK helpers) → assert_testnet → sign_l1_action(is_mainnet=False)
        → DRY_RUN? log + dry_run_orders.jsonl : POST /exchange
```

Alerts and drafts never send anything themselves. They put a menu on the glasses, and every
action they offer goes through the same two taps and the same `submit()`.

## Data flow (one price tick)

1. Hyperliquid pushes `activeAssetCtx{ETH}` (~1 Hz) or `allMids` (~every 5 s on testnet).
2. `feed.py` decodes it → `apply_ws_message()` → `MarketState.update_*()`.
3. If a *watched* coin (ETH + open-position coins) changed, `MarketState` bumps `version` and calls
   every listener — one per connected glasses client (`ClientSession.wake`).
4. Each client's sender task waits until ≥ 500 ms since its last frame, then builds one `hud`
   frame from the *current* state (so bursts collapse into one frame) and sends it.
5. The plugin's `link.ts` parses it, `hud.ts` renders ≤ 9 × 46 characters of text (ASCII plus the
   `▲ ━ ●` glyph set), and `glasses.ts` pushes it with `textContainerUpgrade`. One call is in
   flight at a time, the newest text wins, and unchanged text is skipped.
6. **Latency (Day 3):** the frame carries `src_ms`, the backend time the tick arrived. Once its
   text is on the glasses, the plugin sends `{"type":"rendered","src_ms":…}` back.
   `latency.py` keeps p50 / p95 of `now − src_ms`, measured on the backend clock only. In the
   simulator exit check it was ~70–260 ms p50 and ~500–850 ms p95.

With nothing changing, the backend still sends a frame **every 1 s** (heartbeat). The plugin
re-renders every 1 s and shows `STALE` after 3 s without one (BUILD_LOG §26).

**Voice (Day 3):**
1. Hold → `{"type":"voice","action":"start"}`, then the mic's 16 kHz PCM as binary frames.
2. Release → `stop`.
3. `voice.py` transcribes locally (~1.7 s), and `voice_grammar.py` maps it to one command.
4. `ActionFlow.voice()` runs it through the same `_select()` as a menu tap: it arms, the confirm
   screen appears, and the tap still sends.

Input goes the other way: `onEvenHubEvent` → `input.ts` → `{"type":"intent","action":…}` →
`ws.py` hands it to that client's `ActionFlow`, logs it to SQLite, sends `ack` + a fresh frame.

## Closing a position (Day 2)

```
 HUD ──tap──► MENU ──tap "Close 50%"──► CONFIRM ──tap (0.3–5 s later)──► SENDING ──► RESULT ──8 s / any input──► HUD
  ▲            │ swipe: move cursor      │ swipe: cancel → MENU            │ executor.close()   │
  └─2x/Back────┘                         │ 2x: cancel → HUD                │ (background task)  │
                                         └ 5 s timer: "Confirm expired" → MENU                  └ order frame + order_log
```

- The **backend** owns this state machine (`actions.py`), one per WebSocket client. The plugin
  draws `ui` from each frame and maps input by the current screen (`input.ts`).
  - Double tap = `back` everywhere. On the HUD it opens `End session?` (Day 3).
  - The plugin itself exits only on `end` / `summary`, or when the link is down.
- Tap 1 only *arms*: it freezes the exact order (coin, side, size rounded down to `szDecimals`,
  mark at the time) and shows it. Tap 2 sends it, after re-checking that the position is still
  open on the same side and capping a partial close at the live size.
- `trading.py` sends it with `Exchange.market_close(coin, sz, slippage=5%)` from
  hyperliquid-python-sdk: an IOC limit at mid ± 5 % with `reduce_only=True` hard-coded, the side
  taken from the live position. One order at a time (`asyncio.Lock`), 25 s overall timeout.
- The order task is not cancelled if the glasses disconnect; its result is still logged to
  `order_log`.

## Price sources

| Quantity | Hyperliquid source | Used for |
|---|---|---|
| mid | `allMids`, refreshed by `activeAssetCtx.midPx` | uPnL, uPnL % |
| mark | `activeAssetCtx.markPx` | liquidation distance (liquidation triggers on mark), future risk alerts |
| positions | `clearinghouseState{user}` (only if `HL_ACCOUNT_ADDRESS` is set) | everything position-related |

If no `markPx` has arrived for a coin, `mark` falls back to mid and the frame says
`mark_source: "mid"` (the HUD appends `(mid)`). `webData2` was the original plan; it no longer
exists on the API — see `docs/BUILD_LOG.md` §0/§2.

## Concurrency model

- Backend: **one asyncio event loop**, no threads (except `asyncio.to_thread` for SQLite writes).
  The Hyperliquid client is asyncio-native (`websockets`), so state updates need no locks.
- Per WebSocket client: one sender task + the receiver loop; a per-connection `asyncio.Lock`
  serialises sends.
- Telegram runs in the same loop via python-telegram-bot's `Application` + `Updater` (long polling).

## Failure handling

| Hop | Detection | Recovery |
|---|---|---|
| Backend ↔ Hyperliquid | socket error, or no message for 30 s | reconnect with 1 → 30 s backoff, resubscribe everything; HUD shows "Hyperliquid feed reconnecting..." |
| Plugin ↔ backend, silent | no frame for > 3 s (the backend heartbeats every 1 s) | HUD shows `STALE: no update for Ns` and `!` instead of `●`; from 8 s the watchdog drops the socket and reconnects (`- reconnecting`) |
| Plugin ↔ backend, closed | `onclose` | reconnect with 1 → 15 s backoff; HUD shows the countdown; push-to-talk is reset (mic off) |
| Prices quiet | `feed.age_ms` + frame age > 3 s | HUD shows `STALE: prices Ns old` |
| App backgrounded / foregrounded | `FOREGROUND_EXIT` / `FOREGROUND_ENTER` | mic off on exit; on enter, force a reconnect and re-arm push-to-talk |
| Session started before the feed | `positions_loaded` false, or a position without a mid | the session's uPnL baseline waits for both (an existing position's uPnL never counts as session P&L) |
| Wrong pairing code | close code 4401 | stop retrying, HUD says "Pairing code rejected" |
| Telegram token missing/invalid | at startup | bot skipped, backend keeps running, `/healthz` says why |
| Telegram slow / 429 at startup | `TimedOut` / `RetryAfter` | retried in the background, 5 s doubling to 300 s, never below `retry_after` |
| Telegram long poll dropped | `NetworkError` while polling | PTB reconnects; one warning line (`on_polling_error`) |
| Hyperliquid `/exchange` unreachable at startup | trading warm-up fails | `trading.status = error`; the first close tries again |
| Order gets no reply in 25 s | `asyncio.wait_for` timeout | result "No reply in 25s - CHECK positions" (it may still have gone through) |
| Position closed/flipped between the two taps | re-check on tap 2 | "Position changed - NOT sent" |

## Security & safety boundaries

- **Testnet only**: `assert_testnet()` compares `HL_API_URL` with
  `hyperliquid.utils.constants.TESTNET_API_URL` (exact match) at app creation and again in the feed.
  The backend refuses to start otherwise.
- **No keys in the plugin**: the plugin only knows the backend origin and a pairing code. All
  third-party calls (Hyperliquid, Telegram, Anthropic) are made by the backend.
- **Pairing code** gates `/ws`; it is redacted from backend logs; the dev copy in
  `plugin/.env.local` is stripped from production builds.
- **Agent key only**: orders are signed with an API/agent wallet key (`HL_AGENT_KEY`), never the
  main wallet key. Agent wallets can trade but cannot withdraw. The key is validated (64 hex
  chars) without being echoed, turned into a signer once, and only the agent's public address
  is logged. Trading is on only when both `HL_ACCOUNT_ADDRESS` and `HL_AGENT_KEY` are set.
- **Reduce-only, two-tap**: the glasses' own menus (and voice) only offer reduce-only closes and
  tighter stops. Opening is possible only from your own forwarded instruction, and it always
  needs an SL and passes the limits. Every order passes the policy guard and the server-side
  two-tap confirm (see "Closing a position"). Voice only arms.
- **Testnet, three times**: `assert_testnet` at app creation, in the feed, and when the executor is
  built; the executor also re-checks the SDK object's `base_url` before its first order.
- **Privacy**: Telegram Bot API only (no userbot / MTProto); the bot only sees chats the user
  explicitly connects via Telegram Business; message text is never logged or stored.

## Where things are configured

| What | Where |
|---|---|
| Secrets, account, pairing code, CORS, DB path | `/.env` (template `/.env.example`) |
| Backend URL + dev pairing code for the plugin | `plugin/.env.local` (template `plugin/.env.example`) |
| Network whitelist, permissions, package id, versions | `plugin/app.json` |
| Throttle / heartbeat | `backend/src/tripwire/ws.py` |
| Menu items, confirm window, bounce gap, result hold | `MENU`, `CONFIRM_WINDOW_S`, `MIN_CONFIRM_GAP_S`, `RESULT_HOLD_S` in `backend/src/tripwire/actions.py` |
| Slippage cap, order timeouts | `SLIPPAGE`, `HTTP_TIMEOUT_S`, `ORDER_TIMEOUT_S` in `backend/src/tripwire/trading.py` |
| Footers / screen text | `FOOTERS` and the `*Lines()` functions in `plugin/src/hud.ts` |
| Feed ping / staleness / backoff | `backend/src/tripwire/hl/feed.py` |
| HUD layout and limits | `plugin/src/hud.ts` |
| Swipe direction | `SWIPE_TO_ACTION` in `plugin/src/input.ts` |
| Voice model, push-to-talk limits | `VOICE_ENABLED`, `VOICE_MODEL` in `.env`; `MAX_TALK_MS`, `TAP_GUARD_MS` in `plugin/src/voice.ts`; guards in `backend/src/tripwire/voice.py`; the vocabulary in `voice_grammar.py` |
| STALE threshold, render tick, watchdog | `STALE_MS` in `plugin/src/hud.ts`, `CLOCK_TICK_MS` in `main.ts`, `WATCHDOG_MS` in `link.ts`; `HEARTBEAT_S` in `ws.py` |
| Glyphs (`▲ ━ ●` vs `/!\ - *`) | `GLYPHS` in `.env`; `GLYPH_SETS` in `plugin/src/hud.ts` |
| Journal channel | `JOURNAL_CHAT_ID` in `.env` |
| Dev commands | `/package.json` scripts |

## Next (not built yet)

- First live orders on a funded testnet account (funds pending). Run `npm run golive:check`,
  then follow [GO_LIVE.md](GO_LIVE.md): close → SL move (the `positionTpsl` size convention) →
  forwarded open.
- The demo video on a real testnet position ([DEMO.md](DEMO.md)).
- Telegram Business inbox and replies (need Premium). The owner DM, `/summary` and the journal
  post are verified on real Telegram (BUILD_LOG §28).
- Real glasses: swipe direction, the `▲ ━ ●` glyphs and the `━` rule width on hardware, and the
  G2 microphone for push-to-talk.
- If Hyperliquid testnet ever becomes unusable: [FALLBACK_BYBIT.md](FALLBACK_BYBIT.md).
- Day 6: per-module docs, decision log, how-to guide.
