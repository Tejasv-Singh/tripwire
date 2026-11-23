# Architecture

Tripwire is a trading-session risk co-pilot: live Hyperliquid **testnet** positions on Even
Realities G2 glasses, navigated with ring/temple taps, plus a Telegram Business bot for messages
and (later) alerts. Day-1 scope: read-only HUD + bot skeleton. No orders are placed yet.

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
 └────────────────────────────────────────┼─────────────────────┘
                                          │  HTTPS / WSS
                          ┌───────────────▼────────────────┐
                          │ ngrok  caucus-strategic-endorse │
                          │        .ngrok-free.dev → :8000  │
                          └───────────────┬────────────────┘
 ┌─────────────────────────── laptop ─────┼───────────────────────────────────────────────┐
 │ backend/  FastAPI + uvicorn (Python 3.12, one asyncio event loop)                       │
 │   main.py   create_app(): testnet guard → state → feed task → bot → routes              │
 │   ws.py     /ws  (pairing, hud frames ≤ 2/s, heartbeats, intents)                       │
 │   hud.py    MarketState → JSON frame                                                    │
 │   state.py  MarketState (mids, marks, positions, listeners)                             │
 │   hl/feed.py      ── wss://api.hyperliquid-testnet.xyz/ws ──► Hyperliquid TESTNET       │
 │   hl/positions.py pure math: uPnL, uPnL %, liq distance                                 │
 │   telegram_bot.py ── long polling ──► api.telegram.org (Bot API)                        │
 │   db.py     SQLite: business_connections, intent_log (metadata only)                    │
 └─────────────────────────────────────────────────────────────────────────────────────────┘
```

## Data flow (one price tick)

1. Hyperliquid pushes `activeAssetCtx{ETH}` (~1 Hz) or `allMids` (~every 5 s on testnet).
2. `feed.py` decodes it → `apply_ws_message()` → `MarketState.update_*()`.
3. If a *watched* coin (ETH + open-position coins) changed, `MarketState` bumps `version` and calls
   every listener — one per connected glasses client (`ClientSession.wake`).
4. Each client's sender task waits until ≥ 500 ms since its last frame, then builds one `hud`
   frame from the *current* state (so bursts collapse into one frame) and sends it.
5. The plugin's `link.ts` parses it, `hud.ts` renders ≤ 9 × 46 ASCII text, `glasses.ts` pushes it
   with `textContainerUpgrade` (one call in flight; newest text wins; unchanged text skipped).

Input goes the other way: `onEvenHubEvent` → `input.ts` → `{"type":"intent","action":…}` →
`ws.py` updates that client's selection, logs it to SQLite, sends `ack` + a fresh frame.

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
| Plugin ↔ backend | `onclose`, or no frame for 15 s | reconnect with 1 → 15 s backoff; HUD shows the countdown |
| App backgrounded | `FOREGROUND_ENTER_EVENT` | force reconnect immediately |
| Wrong pairing code | close code 4401 | stop retrying, HUD says "Pairing code rejected" |
| Telegram token missing/invalid | at startup | bot skipped, backend keeps running, `/healthz` says why |

## Security & safety boundaries

- **Testnet only**: `assert_testnet()` compares `HL_API_URL` with
  `hyperliquid.utils.constants.TESTNET_API_URL` (exact match) at app creation and again in the feed.
  The backend refuses to start otherwise.
- **No keys in the plugin**: the plugin only knows the backend origin and a pairing code. All
  third-party calls (Hyperliquid, Telegram, Anthropic) are made by the backend.
- **Pairing code** gates `/ws`; it is redacted from backend logs; the dev copy in
  `plugin/.env.local` is stripped from production builds.
- **Agent key only** (Day 2+): signing will use an API/agent wallet key (`HL_AGENT_KEY`), never the
  main wallet key. Agent wallets can trade but cannot withdraw.
- **Privacy**: Telegram Bot API only (no userbot / MTProto); the bot only sees chats the user
  explicitly connects via Telegram Business; message text is never logged or stored.

## Where things are configured

| What | Where |
|---|---|
| Secrets, account, pairing code, CORS, DB path | `/.env` (template `/.env.example`) |
| Backend URL + dev pairing code for the plugin | `plugin/.env.local` (template `plugin/.env.example`) |
| Network whitelist, permissions, package id, versions | `plugin/app.json` |
| Throttle / heartbeat | `backend/src/tripwire/ws.py` |
| Feed ping / staleness / backoff | `backend/src/tripwire/hl/feed.py` |
| HUD layout and limits | `plugin/src/hud.ts` |
| Swipe direction | `SWIPE_TO_ACTION` in `plugin/src/input.ts` |
| Dev commands | `/package.json` scripts |

## Next (not built yet)

- Day 2: ROE on margin next to uPnL % (`TODO(Day 2)` in `hl/positions.py`); actions menu on tap;
  reduce-only orders via the SDK's `Exchange` with the agent key, behind a two-tap confirm.
- Risk alerts (liq distance thresholds on mark) to glasses + Telegram.
- Day 6: per-module docs, decision log, how-to guide.
