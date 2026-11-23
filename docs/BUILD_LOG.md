# Build log

A running, chronological record of what was built, why, what deviated from the plan, and where
each knob lives. Newest milestone at the bottom. If you need to tweak something, search this file
for the topic first — every non-obvious choice is written down here with the file that owns it.

---

## 2026-11-23 · Day 1

### 0. Research before writing code

What was checked against the real packages/network (not memory):

| Thing | Finding | Consequence |
|---|---|---|
| `@evenrealities/even_hub_sdk@0.0.16` | Requires Even App **2.2.10+**. Events: `CLICK_EVENT=0`, `SCROLL_TOP_EVENT=1`, `SCROLL_BOTTOM_EVENT=2`, `DOUBLE_CLICK_EVENT=3`, `FOREGROUND_ENTER_EVENT=4`, `FOREGROUND_EXIT_EVENT=5`. | `app.json` `min_app_version` = `2.2.10`, `min_sdk_version` = `0.0.16`. |
| SDK wire quirk | `CLICK_EVENT` is `0` and protobuf omits zero-valued fields, so a tap arrives with `eventType: undefined`. | Plugin resolves the default *inside* the envelope check (copied from the asr template's comment). |
| `asr` template | Vite 5 + TS, `createStartUpPageContainer` with one 576×288 text container, 120 ms render debounce, double-tap → `shutDownPageContainer(1)`. STT is a throwing stub. | We keep the structure, drop the STT stub, keep the `g2-microphone` permission for later voice features. |
| `hyperliquid-python-sdk==0.24.0` | `WebsocketManager` only routes a fixed list of channels; **no `clearinghouseState`, no `webData3`**; no auto-reconnect (`run_forever()` once, on a thread). | See "Hyperliquid streaming" below. |
| Testnet WS, live probe | `webData2` subscribe is **rejected** (`"Error parsing JSON into valid websocket request"`) — the channel is gone. `clearinghouseState{user}` works (push ~every 5 s). `webData3` works but carries no asset contexts. `activeAssetCtx{coin}` works, ~1 Hz, contains `markPx` and `midPx`. `allMids` pushes only every ~5 s on testnet. | Mark price comes from `activeAssetCtx` instead of webData2. |
| Python | 3.12.5 installed (no 3.11). | Backend targets **Python 3.12** (user-approved change). |
| `make` | Not installed on the Windows dev box. | Dev commands are root **npm scripts** instead of a Makefile. |

### 1. Repo skeleton

- `git init -b main` (local only — no GitHub remote until the user creates `Tejasv-Singh/tripwire`).
- `.gitignore`: ignores `.env`, `.env.*` (except `.env.example`), `*.local`, venvs, caches,
  `backend/data/` (SQLite), `node_modules/`, `dist/`, `*.ehpk`.
- `LICENSE`: MIT, © 2026 Tejasv Singh Hada.
- `/.env.example`: every backend variable with a one-line "where to get it" comment.
- `.env` was created locally from `.env.example` with a freshly generated `PAIRING_CODE`
  (`secrets.token_urlsafe(12)`); every other value is left empty for the user to fill in.

### 2. Backend core: config, testnet guard, position math

Files: `backend/pyproject.toml`, `backend/src/tripwire/config.py`, `backend/src/tripwire/hl/positions.py`,
`backend/tests/test_config.py`, `backend/tests/test_positions.py`.

- **Tooling**: `uv` project, `requires-python = ">=3.12,<3.13"`, `.python-version` = `3.12`.
  `hyperliquid-python-sdk` pinned to `==0.24.0`; everything else has lower bounds and is pinned by `uv.lock`.
  Dev tools: pytest (+asyncio, httpx), ruff, mypy (`strict`).
- **Config** (`config.py`): pydantic-settings reads **the repo-root `.env`** (`REPO_ROOT / ".env"`).
  Secrets are `SecretStr`. Validation: `PAIRING_CODE` ≥ 8 chars (required); `HL_ACCOUNT_ADDRESS`
  empty or `0x` + 40 hex (lower-cased). `CORS_ORIGINS` is a comma-separated string →
  `cors_origin_list`. `DB_PATH` relative paths resolve from `backend/`.
- **Testnet guard** (`assert_testnet` in `config.py`): exact string equality with
  `hyperliquid.utils.constants.TESTNET_API_URL`. No normalisation — a trailing slash is refused
  too. Called (a) when the app is created, before anything starts, and (b) again inside the
  Hyperliquid feed constructor (defence in depth). Raises `TestnetGuardError`.
- **Position math** (`hl/positions.py`, pure functions, no I/O):
  - `upnl = szi × (mid − entry)` — `szi` is signed, so shorts come out right automatically.
  - `upnl_pct = upnl / (|szi| × entry) × 100` — **on entry notional** (matches the mock:
    41.20 / 1,706 = 2.4 %). `TODO(Day 2)`: also show ROE on margin as a second figure.
  - `liq_dist_pct` = `(mark − liq)/mark` for LONG, `(liq − mark)/mark` for SHORT, ×100.
    Positive = buffer left; ≤ 0 = at/through liquidation; `None` if no liq price.
  - Positions sorted by coin so next/prev order doesn't jump around as prices move.
  - Normalised object = the spec'd fields `{coin, side, size, entry, mark, upnl, upnl_pct,
    liq_px, liq_dist_pct, leverage}` **plus** `mid`, `mark_source` (`"markPx"` or `"mid"`),
    `leverage_type`.
- **Tests**: 28 passing (`uv run pytest` in `backend/`).

#### Price sources (user-approved change #2, adapted to what the API actually offers)

| Quantity | Source | Used for |
|---|---|---|
| `mid` | `allMids` (every ~5 s on testnet), refreshed by `activeAssetCtx.midPx` (~1 Hz) — same quantity, fresher | live uPnL, reference price on the HUD |
| `mark` | `activeAssetCtx.markPx` (~1 Hz) | liquidation distance, (later) risk alerts |
| fallback | if no `markPx` yet for a coin → `mark = mid`, `mark_source = "mid"` | — |

**Deviation:** the plan said "markPx from webData2 asset contexts". `webData2` no longer exists on
the testnet WS (verified: the server rejects the subscription), and its successor `webData3` has
no asset contexts. `activeAssetCtx` carries the very same `markPx` field per coin, so we subscribe
to it for ETH (the reference coin) and for every coin with an open position. The mid fallback is
implemented in `pick_mark()` and visible on the wire as `mark_source`.

### 3. Backend services: live feed, HUD socket, health, Telegram, SQLite

Files: `backend/src/tripwire/{state,hud,ws,db,telegram_bot,main}.py`, `backend/src/tripwire/hl/feed.py`,
tests `test_state_feed.py`, `test_hud.py`, `test_app.py`, `test_db.py`.

**Hyperliquid streaming** (`hl/feed.py`) — deviation from "use the SDK's WebSocket":
the SDK's `WebsocketManager` cannot route `clearinghouseState` (and `webData2` is gone), runs on
a thread and never reconnects. So the feed is a ~150-line **asyncio-native client on the same wire
protocol** using `websockets` (already installed via `uvicorn[standard]`). The SDK is still used for
`constants.TESTNET_API_URL` (the guard) and will be used for signed `Exchange` calls with the agent
key on Day 2.

| Knob | Where | Default | Why |
|---|---|---|---|
| Subscriptions | `HyperliquidFeed._subscribe_initial` / `_sync_ctx_subscriptions` | `allMids`; `activeAssetCtx` for ETH + each position coin; `clearinghouseState` if an account is set | see "Price sources" |
| App-level ping | `PING_EVERY_S` | 30 s | HL closes sockets that send nothing for 60 s |
| Staleness watchdog | `STALE_AFTER_S` | 30 s | no message at all → force reconnect |
| Reconnect backoff | `run()` | 1 s doubling to `BACKOFF_MAX_S` = 30 s | reset after a successful connect |
| Max WS message | `MAX_MSG_BYTES` | 8 MiB | `allMids` lists ~3.5k coins |

Message routing is a pure function `apply_ws_message(state, msg)` so it is unit-tested without a
network. `clearinghouseState` pushes for a different `user` are ignored.

**Market state** (`state.py`): single-event-loop store (no threads, no locks). Stores *all* mids
(so a new position has a price immediately) but wakes listeners only when a *watched* coin
(reference coin ETH + open-position coins) changes. `version` counter + listener callbacks.

**HUD frame** (`hud.py`): backend sends **structured data**, the plugin does the text layout.
Selection (`view.index`) is **per connection**, so two clients (simulator + phone) can browse
independently. Frame schema → `docs/ws-protocol.md`.

**`/ws`** (`ws.py`):
- Auth: `?code=` compared with `secrets.compare_digest`. Wrong code → accept, then close with
  **4401** `"bad pairing code"` (a pre-accept reject would surface in the browser as a reasonless
  1006).
- On connect: `hello` frame, then a `hud` frame immediately.
- Throttle: frames at most every **500 ms** (`MIN_INTERVAL_S`); a burst of changes coalesces into
  one frame with the latest state. Heartbeat frame every **5 s** (`HEARTBEAT_S`) even with no
  change. Both are `create_app(min_interval_s=…, heartbeat_s=…)` parameters.
- Intents validated by pydantic (`IntentMessage`): `next`/`prev` wrap around the position list;
  `actions` is logged only (Day 1). Every intent → `ack`; anything else → `error` `bad_message`.
  All intents are written to SQLite `intent_log`.
- One `asyncio.Lock` per connection serialises sends (sender task + receiver loop both send).

**`/healthz`** (`main.py`): `ok`, `network`, Hyperliquid connected / last message age / reconnects /
position count, Telegram status + detail, number of WS clients. Never returns the address or secrets.

**CORS** (`main.py`): `CORS_ORIGINS` (default `http://localhost:5173,http://127.0.0.1:5173`),
`GET` only, allowed request headers `ngrok-skip-browser-warning` and `content-type` (the plugin sends
the ngrok header on every fetch, which triggers a preflight). WebSockets are not subject to CORS;
the pairing code is the WS auth.

**Telegram** (`telegram_bot.py`, python-telegram-bot **22.8**, Bot API only):
- Long polling inside the FastAPI process (no public webhook needed), `allowed_updates` =
  message, business_connection, business_message, edited_business_message,
  deleted_business_messages; `drop_pending_updates=True` on start.
- `/start` → intro text. `business_connection` → log + upsert SQLite. `business_message` → log
  **metadata only** (connection id, chat id, message id, has_text, length). Forwarded private
  message → echo.
- Empty `TELEGRAM_BOT_TOKEN` → status `disabled`; invalid token / Telegram unreachable → status
  `error` with the reason in `/healthz`; the backend keeps running either way.
- PTB 22 replaced `BusinessConnection.can_reply` with `rights.can_reply`; we read the latter.

**SQLite** (`db.py`): `business_connections` (ids, enabled, can_reply, timestamps) and
`intent_log` (ts, action, client). **No message-text column** — a test asserts this. File:
`DB_PATH` (default `backend/data/tripwire.sqlite3`, gitignored). Short-lived connection per call,
run via `asyncio.to_thread`.

**Secret redaction in logs** (`main.py`, found during the live smoke test): uvicorn logs the WS
handshake as `"WebSocket /ws?code=<PAIRING_CODE>" [accepted]`, and httpx logs Telegram API URLs
that contain the bot token. `RedactSecretsFilter` rewrites `code=…` → `code=***` and
`bot<token>` → `bot***` on the `uvicorn.error`, `uvicorn.access`, `httpx`, `telegram` and
`tripwire` loggers; httpx is also lowered to WARNING. The pairing code that appeared in the first
smoke-test log was rotated. ⚠️ ngrok's own request inspector (`http://127.0.0.1:4040`) still shows
full URLs including `?code=` — it's local to your machine, but don't screen-share it.

**App factory**: run as `uvicorn tripwire.main:create_app --factory`. The factory reads `.env`,
runs `assert_testnet()` **before** creating anything, then wires state/feed/bot/router. Lifespan
starts the feed task and the bot and stops both on shutdown.

**Live smoke test (2026-11-23, no account set)**: `/healthz` → Hyperliquid connected; `/ws` →
`hello`, then `hud` frames with ETH `mid` and `mark` (`mark_source: "markPx"`), heartbeat every
5 s when nothing changes. Testnet ETH can sit still for several seconds — a frame with an unchanged
price is a heartbeat, not a stuck feed (watch `seq` and `feed.age_ms`).

**Checks**: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (strict),
`uv run pytest` → **59 passed**. (One harmless `StarletteDeprecationWarning` from FastAPI's
TestClient about `httpx` — upstream, ignore.)

### 4. Plugin (Even Hub, Vite + TypeScript)

Scaffolded with `npx degit even-realities/evenhub-templates/asr plugin`, then:

| Change vs. template | Why |
|---|---|
| `@evenrealities/even_hub_sdk` pinned to exactly **`0.0.16`** (template had `^0.0.10`) | spec; reproducible |
| `evenhub-cli` `0.2.0`, `evenhub-simulator` `0.9.5` pinned exactly; `vitest` added | current versions; tests |
| `src/asr/stt.ts` and all mic/STT wiring removed; template `README.md` removed | Tripwire doesn't use STT yet; the template README described STT only |
| `g2-microphone` permission **kept** in `app.json` (desc says "reserved") | user asked for `["network","g2-microphone"]`; voice commands later |
| `vite.config.ts`: `strictPort: true`, vitest config | 5173 is assumed by the simulator, CORS and docs |
| `simulate` script calls `node node_modules/@evenrealities/evenhub-simulator/bin/index.js` | on this machine `npm install` did **not** create the `.bin/evenhub-simulator` link (an `npm rebuild` fixed it, but a fresh clone could hit it again) |
| `simulate:auto` adds `--automation-port 9898` | the simulator's HTTP API (`/api/screenshot/glasses`, `/api/input`, `/api/console`) — used to verify the exit check |
| `pack` writes `tripwire.ehpk` (`-o`) | instead of the default `out.ehpk`; `*.ehpk` is gitignored |

**`app.json`**: `package_id` `com.tejasvsingh.tripwire`, name `Tripwire`, `min_app_version`
`2.2.10` (required by SDK 0.0.16), `min_sdk_version` `0.0.16`, permissions `network` (whitelist:
`https://caucus-strategic-endorse.ngrok-free.dev` **and** `wss://caucus-strategic-endorse.ngrok-free.dev`)
and `g2-microphone`. `evenhub pack app.json dist` accepts it (verified; produced a 41 KB `.ehpk`).
⚠️ If the backend host changes, update **both** the whitelist and `VITE_BACKEND_URL`.

**Config** (`src/config.ts`, `plugin/.env.example` → copy to `plugin/.env.local`, gitignored):
- `VITE_BACKEND_URL` — backend origin; the WS URL is derived (`https→wss`, `+ /ws?code=…`).
  Not hard-coded in source.
- `VITE_PAIRING_CODE` — **dev only**. Guarded by `import.meta.env.DEV`, so `vite build` drops it:
  verified by grepping `dist/` for the code after a build (not found). In production the code is
  typed into the companion page and saved with `bridge.setLocalStorage('tripwire.pairingCode', …)`;
  stored value wins over the dev value.
- `plugin/.env.local` was created locally with the ngrok URL and the pairing code from `/.env`.

**Modules** (`plugin/src/`):

| File | Responsibility |
|---|---|
| `main.ts` | Boot: companion UI → `waitForEvenAppBridge()` → load pairing code → create glasses page → open WS → route input → 10 s clock tick → 30 s `/healthz` poll |
| `glasses.ts` | Creates **one** page with **one** 576×288 text container (`isEventCapture: 1`), then only ever calls `textContainerUpgrade`. Latest-wins queue: one upgrade in flight at a time, newest pending text sent next, identical text skipped. No page rebuilds for live numbers. |
| `hud.ts` | Pure renderer `renderHud({frame, link, now, frameAgeMs})` → text. ASCII only, ≤ 46 cols, ≤ 9 lines; `fit()` is a last-resort sanitiser (non-ASCII → `?`, overflow → `~`). |
| `link.ts` | WebSocket client: backoff 1 s → 15 s, watchdog (no frame for 15 s → reconnect; backend heartbeats every 5 s), close code 4401 → "rejected" (no retry loop), `reconnectNow()` for FOREGROUND_ENTER / new code. |
| `input.ts` | Pure event → command mapping (table `SWIPE_TO_ACTION`). |
| `api.ts` | `backendFetch()` — **every** HTTP call goes through it and gets `ngrok-skip-browser-warning: true`. `fetchHealth()` fails loudly if the reply isn't JSON (ngrok's warning page is HTML). |
| `protocol.ts` | Wire types mirrored from the backend + `parseServerFrame()` (rejects non-JSON). |
| `ui.ts` | Companion page: status chip, backend + health, **mirror of the glasses text**, pairing-code field, rolling log (also `console.log` with a `[tripwire]` prefix). |

**Input mapping** (`input.ts`):

| Glasses / ring event | Command |
|---|---|
| `SCROLL_BOTTOM_EVENT` (swipe down) | intent `next` |
| `SCROLL_TOP_EVENT` (swipe up) | intent `prev` |
| `CLICK_EVENT` (tap; arrives as `eventType: undefined`) | intent `actions` (logged only on Day 1) |
| `DOUBLE_CLICK_EVENT` | `shutDownPageContainer(1)` → OS exit confirmation (checked first, always) |
| `FOREGROUND_ENTER_EVENT` | `link.reconnectNow()` |
| `SYSTEM_EXIT_EVENT` / `ABNORMAL_EXIT_EVENT` | close WS, unsubscribe |

Swipe direction is a guess from the event names; if it feels inverted on hardware, swap the two
entries in `SWIPE_TO_ACTION`.

**HUD layout** (`hud.ts`; exact mock reproduced in `hud.test.ts`):
```
TRIPWIRE            TESTNET      HH:MM
ETH-PERP LONG 0.50 @ 3,412  5x          (+ "  2/3" when several positions)
uPnL +$41.20 (+2.4%)
Liq 2,980  12.7% away                   ("Liq none", "AT/PAST LIQ", "(mid)" when no markPx)
----------------------------------------
tap: actions  swipe: next  2x: exit
```
No position: line 2 = `ETH-PERP  1,721.9  mark` (or `mid` if no markPx yet), line 3 =
`No account set (price only)` / `No open position`. Link problems replace the body lines
(`Connecting...`, `Backend unreachable. Retrying in Ns...`, `Pairing code rejected.`, `Not paired.`).
Number formats: live prices at 5 significant figures (Hyperliquid's own precision), entry/liq at 4,
size `0.50`-style, uPnL `+$41.20`, pct one decimal. Clock is the phone's local time.
The G2 font is proportional, so the space-padding in the header is approximate on hardware.

**Checks**: `npx tsc --noEmit` clean; `npx vitest run` → **17 passed**; `npm run build` OK.
`npm install` reports audit advisories in dev-only tooling (Vite 5 / esbuild dev server) — not
shipped in the `.ehpk`; revisit when moving to Vite 6+.

### 5. Root dev scripts (`/package.json`)

No `make` on Windows, so the monorepo commands are npm scripts:

| Script | Runs |
|---|---|
| `npm run setup` | `uv sync` in `backend/` + `npm install` in `plugin/` |
| `npm run backend:dev` | uvicorn factory on `127.0.0.1:8000` with `--reload` (watches `backend/src`) |
| `npm run backend:start` | same without reload |
| `npm run plugin:dev` | Vite dev server on `:5173` |
| `npm run sim` / `sim:auto` | simulator → `http://localhost:5173` (`:auto` adds automation API on `:9898`) |
| `npm run tunnel` | `ngrok http --url=caucus-strategic-endorse.ngrok-free.dev 8000` |
| `npm test` / `npm run check` | all tests / tests + ruff + mypy + tsc |
