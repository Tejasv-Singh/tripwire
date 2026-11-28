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

### 6. Local end-to-end check (backend + Vite + simulator, no tunnel)

Run: backend on `127.0.0.1:8000`, Vite with `VITE_BACKEND_URL=http://127.0.0.1:8000` (an
environment variable overrides `.env.local`), simulator with `--automation-port 9898`.

- **First attempt: blank display.** The simulator loaded `http://localhost:5173` before Vite was
  listening and showed "localhost refused to connect". **Start Vite first and wait for "ready"**
  (now in the README run order and troubleshooting table). The simulator doesn't retry.
- **Second attempt:** the console showed `Bridge initialized`, `[tripwire] ws open`,
  `hello: protocol v1, testnet`, and the glasses framebuffer (`GET /api/screenshot/glasses`,
  an RGBA PNG composited onto black to view it) showed:
  ```
  TRIPWIRE            TESTNET      17:00
  ETH-PERP  1,724.9  mark
  No account set (price only)
  ----------------------------------------
  tap: actions  swipe: next  2x: exit
  ```
- **Live updates:** six framebuffer captures about 8 s apart read ETH mark 1,725.7 → 1,726.7 →
  1,725.7 → 1,725.2 → 1,724.6 → 1,724.2, and the clock went 17:00 → 17:01. ✅
- **Input:** `POST /api/input` with `down`, `up` and `click` gave `intent next`, `intent prev` and
  `intent actions` in the plugin log. The backend logged all three and wrote them to `intent_log`,
  and each was acked. So the simulator's **down = `SCROLL_BOTTOM_EVENT`**, which confirms the
  swipe mapping (still to be checked on hardware).
- Fixed: the ack log line printed `1/0` when there were no positions; it now says `no positions`.
- **Hyperliquid expires sockets.** After about 12 minutes the testnet server closed our WS with
  `1000 (OK) Expired`. The feed logged it, reconnected 1.5 s later and resubscribed
  (`/healthz` → `reconnects: 1`). Expect this regularly. It isn't a bug, and the HUD only shows
  "feed reconnecting" for those ~1–2 s.
- When stopping background `npx vite`, the child `node …/vite.js` kept port 5173 open, and
  `strictPort` would then make the next `plugin:dev` fail. If 5173 is "in use", look for a
  leftover `node … vite.js` process.
- Rendering note: the simulator draws a proportional font, so the header's space padding is
  approximate (as expected).

### 7. Docs

`README.md` (what it is, setup, run order, troubleshooting, safety rules, privacy, what existed
before Nov 23 plus the open-source list with versions and licenses read from the installed
packages), `docs/architecture.md`, `docs/ws-protocol.md`. Per-module pages, a decision log and a
how-to guide are deferred to Day 6. Until then, this build log is where decisions are recorded.

### 8. ngrok install (Windows)

- The winget ID is **`Ngrok.Ngrok`**; `ngrok.ngrok` isn't found with `--exact`.
  `winget install --id Ngrok.Ngrok --exact --source winget` installed **3.3.1** (2023) from
  `bin.equinox.io` (hash verified) and added its folder to the user PATH, so only shells opened
  afterwards see it.
- 3.3.1 predates `ngrok http --url=…`, so `ngrok update` was run straight away and moved it to
  **3.39.13** (`--url` confirmed in `ngrok http --help`). winget still lists 3.3.1. If ngrok is
  ever reinstalled through winget, run `ngrok update` again.
- The authtoken (`ngrok config add-authtoken …`, saved in `%LOCALAPPDATA%\ngrok\ngrok.yml`) is
  added by the user, never by tooling, and is never committed.

### 9. ✅ Day-1 exit check: backend + ngrok tunnel + simulator (2026-11-23, ~17:16 IST)

Setup, exactly as in the README: `npm run backend:dev`, `npm run tunnel` (ngrok 3.39.13, free
plan, region India), `npm run plugin:dev` (`VITE_BACKEND_URL` = the ngrok URL from
`plugin/.env.local`), `npm run sim:auto`.

- ngrok request log: `GET /ws 101 Switching Protocols`, `OPTIONS /healthz 200`, `GET /healthz 200`.
  The simulator's WebView WebSocket **upgraded through ngrok without hitting the warning page**,
  so no Cloudflare tunnel is needed.
- Glasses framebuffer (6 captures, ~8 s apart): ETH mark 1,727.2 → 1,726.1 → 1,727.2, clock
  17:17 → 17:18. Companion page: chip LIVE, health "HL live · telegram disabled · 1 client(s)".
- Swipe down injected via the automation API → `intent next` → backend `ack` through the tunnel.

**ngrok free-tier warning page, measured.** Requests use an Android Chrome User-Agent:

| Request through `https://caucus-strategic-endorse.ngrok-free.dev` | Result |
|---|---|
| `wss://…/ws?code=…`, no skip header (browsers can't send one) | **101**, frames stream normally |
| `GET /healthz`, `Accept: text/html`, **no** `ngrok-skip-browser-warning` | 200 **`text/html`** (warning page) |
| `GET /healthz` **with** `ngrok-skip-browser-warning: true` | 200 `application/json` |

So the header on every `fetch` (`plugin/src/api.ts`) is required, and the WebSocket doesn't need
it. If a future ngrok change starts intercepting WS upgrades, the plugin will show "Backend
unreachable" and the ngrok log will show a non-101 on `/ws`. The fallback is a Cloudflare named
tunnel: change the host in `plugin/app.json` (both entries), `plugin/.env.local` and the `tunnel` script.

Note: through ngrok, the backend logs the client's **public IP** in uvicorn access lines. That's
expected and local only, but don't paste those logs publicly.

### 10. Published to GitHub

- Repo: <https://github.com/Tejasv-Singh/tripwire> (public, created by the user). `origin` =
  `https://github.com/Tejasv-Singh/tripwire.git`, branch `main`.
- **Pre-publish secret scan, all commits:** the only env files ever committed are `.env.example`
  and `plugin/.env.example`, with every secret value empty. The live `PAIRING_CODE` appears in 0
  commits. No hits for Telegram-token, `sk-ant-`, 64-hex private-key, GitHub-token or PEM
  patterns. `.env` and `plugin/.env.local` are gitignored and were never committed.
- **Author email:** before the first push, all 7 local commits were rewritten from the personal
  address to the GitHub noreply address (`git filter-branch --env-filter`). Dates, messages and
  the final tree are unchanged; the tree hash was checked before and after. This repo's
  `git config --local user.email` is now the noreply address, so future commits use it too.
  Global git config is untouched.
- If you clone this on another machine, set the same local email there:
  `git config user.email "<id>+Tejasv-Singh@users.noreply.github.com"` (the id is shown under
  GitHub → Settings → Emails).

---

## 2026-11-24 · Day 2 (morning)

### 11. Telegram bot setup, and making the bot's startup resilient

**Setup, as the user did it:** BotFather `/newbot` → `@tripwire_g2_bot`. In BotFather's current
mini-app, **"Business Mode" is called "Secretary Mode"** (Bot Settings → Mode Settings). Its
description, "read or send messages on behalf of the user who enabled it in Settings", is the
Business-bot feature. Turned on. Connecting the bot to a user's chats (Settings → Telegram
Business → Chatbots) **requires Telegram Premium**; the user confirmed this in the app. Part of the
flow is therefore not testable without Premium: `/start` and the forward echo work on a free
account, but `business_connection` / `business_message` can't be.

**Problem found:** with the token set, startup logged
`Telegram bot skipped: TimedOut: Timed out` and the bot stayed off until a manual restart.
Diagnosis:

| Test | Result |
|---|---|
| `curl https://api.telegram.org/` (IPv4 and IPv6) | 302 in ~1 s, so the network can reach Telegram |
| Bot API `getMe` with a **fake** token | 401 in 0.7 s, so the Bot API path is fine |
| `getMe` with the **real** token, PTB defaults (5 s) | `TimedOut` ×4, then `RetryAfter` |
| `getMe` with the real token, 30–45 s timeout | reply after **~57 s**: `429 Too Many Requests: retry after 32` |

So the problem was specific to this bot. Telegram was holding replies for a brand-new bot (its
first logins can be slow), and every retry, including requests that timed out but still reached
Telegram, re-armed the 32 s flood window. Uvicorn's auto-reload made it worse: each save of
`telegram_bot.py` restarted the backend, which made another call.

**Fix** (`backend/src/tripwire/telegram_bot.py`):
- `TelegramBot.start()` is now **non-blocking**. It spawns a `telegram-bot` task, so backend
  startup is never held up. The new status `starting` (with an attempt/retry detail) shows on
  `/healthz`.
- Retry policy (`retry_delay()`): timeouts, network errors, 409 and 429 are retried; the wait is
  5 s doubling, capped at `RETRY_MAX_S` = 300 s. On a 429 it waits `max(retry_after + 1, backoff)`,
  so gaps keep growing even if Telegram keeps re-arming the window. **`InvalidToken` is permanent**:
  status `error` with a "fix TELEGRAM_BOT_TOKEN" hint, and no retries.
- Per-request timeouts raised from PTB's 5 s to `CONNECT_TIMEOUT_S` = 10 s and `READ_TIMEOUT_S` =
  20 s (`build_application`).
- Failed attempts shut their `Application` down; `stop()` cancels a pending backoff cleanly.
- `TelegramBot` takes `app_factory` and `sleep` parameters so the loop is tested offline
  (`tests/test_telegram_bot.py`, fake Application, no network). The tests cover the backoff and
  429 math, transient errors followed by running, a permanent bad token, a non-blocking start with
  a cancellable stop, and an empty token.
- PTB 22.2+ warns that `RetryAfter.retry_after` will become a `timedelta`. The code handles both
  types and suppresses the warning where it reads the value.

Checks: ruff, ruff format, mypy strict clean; **65 tests pass** (was 59).

**Lesson / how to operate:** when Telegram is throttling, **leave the backend running**. Restarts
and manual `getMe` tests each make another request and extend the throttle. If it doesn't recover
in about 30 minutes, `/revoke` the token in BotFather and use the new one.

**Outcome (2026-11-24, IST):** the bot came up **on its own, with the same token and no `/revoke`**.
After the last backend restart (13:39:46), the retry loop ran like this:

| Attempt | Time | Result | Next wait |
|---|---|---|---|
| 1 | 13:40:08 | `TimedOut` | 5 s |
| 2 | 13:40:15 | `RetryAfter 32` | 33 s |
| 3 | 13:41:10 | `TimedOut` | 20 s |
| 4 | 13:41:32 | `RetryAfter 24` | 40 s |
| 5–8 | 13:42:34 → 13:52:39 | `TimedOut` ×4 | 80 → 160 → 300 → 300 s |
| 9 | 13:57:46 | **`Telegram bot running as @tripwire_g2_bot`** | — |

That's about 18 minutes from the restart to the bot running. Once the gaps between attempts grew
past Telegram's flood window, the next `getMe` got through, which is the behaviour `retry_delay()`
was built for. `/healthz` → `telegram: {"status": "running", "detail": "@tripwire_g2_bot"}`.

Manual tests on a free (non-Premium) account, both passed:
- `/start` → the intro text (`START_TEXT`).
- Forwarding a message from another chat → `Echo: <text>`. The backend log shows only
  `forwarded message chat=… msg=… len=…`, with no message text.
- Not testable without Premium: `business_connection` / `business_message`.

**Known noise (harmless):** while the bot is running, the log sometimes shows
`ERROR telegram.ext.Updater: Exception happened while polling for updates` with a long
`httpx.ReadError` traceback. It happened 3 times in about 50 minutes (14:04, 14:43, 14:46). It means
the `getUpdates` long-poll connection was dropped, and PTB's own `network_retry_loop` reconnects
straight away. `/healthz` stayed `running` and both tests passed after these errors. Possible
follow-up: pass `error_callback=` to `start_polling()` so this prints as one warning line. That
edits `telegram_bot.py`, which triggers a dev-server reload and a new `getMe`, so do it at a
quiet moment. (Done in §12.)

---

## 2026-11-24 · Day 2 (afternoon)

### 12. ROE, actions menu, two-tap reduce-only closes (protocol v2)

Scope came from the "Next" list in `docs/architecture.md`: ROE next to uPnL %, an actions menu
on tap, and reduce-only orders with the agent key behind a two-tap confirm. Risk alerts are still
to do. There's no funded testnet account or agent key yet, so everything was built and tested
against a **fake exchange**, and no request was sent to Hyperliquid's `/exchange`.

**ROE (`hl/positions.py`, `roe_pct`).** `roe_pct = upnl / (size × entry / leverage) × 100`,
which equals `upnl_pct × leverage`.
- This is return on **initial margin**, the same definition as Hyperliquid's own
  `returnOnEquity`. Check: in the test fixture, 41.2 / (1,706 / 5) = 0.1207 = HL's value.
- Not `marginUsed`: on cross positions `marginUsed` = positionValue / leverage moves with the
  price, so ROE would drift even with flat PnL.
- Isolated positions with extra margin added are approximated the same way.
- `roe_pct` is null when leverage is 0/unknown.
- It uses our live `upnl` (on mid), so it moves with the HUD. The HUD shows
  `uPnL +$41.20 (+2.4%)  ROE +12.1%`.

**Who owns the menu: the backend.** `actions.py` has one `ActionFlow` per WebSocket client, with
the screens hud → menu → confirm → sending → result. The plugin only draws `ui` from each frame.
- Why: the two-tap confirm is a safety rule, so it has to hold even for a buggy or replayed
  client. The only path to an order is `tap` (arm) then `tap` 0.3–5 s later.
- Timings: `CONFIRM_WINDOW_S` = 5 s, `MIN_CONFIRM_GAP_S` = 0.3 s (a bounced tap isn't a
  confirm), `RESULT_HOLD_S` = 8 s.
- Timers use `loop.call_later` with a token, so a stale timer can't change a newer screen.
- While a menu is open the client is **locked to that coin** (`hud.resolve_index`). A new
  position sorting in front can't swap what the menu is about, and if the coin closes, `position`
  becomes null instead of another coin.

**Menu items:** Close 25 %, Close 50 %, Close 100 %, Back (`MENU`). No item opens, adds to or
flips a position.

**Orders (`trading.py`).** `HyperliquidExecutor.close()` calls the SDK's
`Exchange.market_close(coin, sz, None, SLIPPAGE=0.05)`. What that call does (read from the 0.24.0
source):
- It reads the **live** `user_state` for `account_address`, so the side always comes from the
  live position.
- It sends an IOC limit at mid × (1 ± 5 %) with `reduce_only=True` hard-coded.
- It returns `None` if there's no position for the coin, which we show as "No open position".
- Signing picks the chain with `base_url == MAINNET_API_URL`, so our testnet URL signs for
  testnet.

How sizes are chosen:
- **Partial closes** (25/50 %) are rounded **down** to the coin's `szDecimals`, taken from the
  SDK's `info.asset_to_sz_decimals[info.name_to_asset(coin)]`. The SDK's `float_to_wire` raises
  on more than 8 decimals, and the exchange rejects sizes that aren't a multiple of the lot.
- The confirm screen shows exactly that rounded size, and tap 2 sends
  `min(shown, live size)`, never more than what was shown.
- **Close 100 %** sends `sz=None`, which makes the SDK close the whole live size.
- A partial that rounds to 0 is refused before anything is sent.

Other executor behaviour:
- Building `Exchange` makes two blocking HTTP calls (meta and spotMeta). It runs in a thread
  as a background **warm-up** at startup, so exact sizes are ready before the first menu.
- `/healthz` → `trading.status` is `off`, `warming`, `ready` or `error`. A failed warm-up isn't
  fatal: the first close retries it.
- One order at a time (`asyncio.Lock`). The whole call has a 25 s timeout, and the requests
  timeout is 10 s. A timeout says **"No reply in 25s - CHECK positions"**, because the worker
  thread may still deliver the order.
- Replies are parsed by `parse_order_response`: `filled` (totalSz/avgPx/oid), `resting`,
  `{"error"}` → `rejected`, `status: err` → `rejected`. SDK exceptions use `ClientError.error_message`.
  Messages are trimmed to 120 characters, and the plugin word-wraps them to 3 lines.
- The order task is **not cancelled if the glasses disconnect**, because the order may already be
  out. The result still goes to SQLite `order_log` (new table: ts, client, coin, side, fraction,
  size, ok, status, filled_size, avg_px, oid, message) and, if still connected, to a new `order`
  frame.

**Agent key handling (`config.py`, `trading.py`).**
- `HL_AGENT_KEY` must be empty or 64 hex characters (`0x` optional). The validation error never
  echoes the value.
- `Settings.trading_enabled` needs **both** `HL_ACCOUNT_ADDRESS` and `HL_AGENT_KEY`; otherwise
  `DisabledExecutor` is used. The menu still opens, but picking a close shows "Trading off", never
  a confirm.
- The key is turned into an `eth_account` signer once, and only `agent_address` (public) is kept
  as an attribute and logged as `0x1234...abcd`. A test checks that no attribute's repr contains
  the key.
- Testnet is checked again: `assert_testnet` runs in the executor's constructor, and the built
  `Exchange.base_url` is re-checked before the first order. That makes three places, with
  `create_app` and the feed.

**Protocol v2** (`docs/ws-protocol.md` has the full reference):
- Intents are `next | prev | tap | back`; v1's `actions` now gets `bad_message`.
- `hud` frames gain `ui` and `trading`, `position` gains `roe_pct`, and `hello` gains `trading`.
- `ack` gains `mode` and can be `ok: false` with a `note`.
- There's a new `order` frame.

**Plugin.**
- `input.ts`: `commandFromEvent(event, mode)`. A double tap **exits on the HUD and sends `back`
  everywhere else**, so two double taps always leave the app. A tap is always `tap`.
- `main.ts` passes `currentMode(frame, linkView)`, which is always `hud` while the link is down,
  so a stale menu frame can't stop you exiting.
- `hud.ts` draws the menu, confirm, sending and result screens, with a footer per screen
  (`FOOTERS`).
- The body is capped at 6 lines, so the footer can't be pushed off: header + 6 + separator +
  footer = 9 lines.
- On the confirm screen, the verb is SELL when closing a long and BUY when closing a short. It
  shows `ALL` for 100 % and `~` when the size isn't rounded yet. New helpers: `fmtQty` (all
  decimals, up to 8) and `wrap`.

**Telegram polling noise.** PTB's `Updater` logs every `getUpdates` failure via
`default_error_callback`, which calls `_LOGGER.exception`: an ERROR with a 40-line traceback.
`start_polling(error_callback=on_polling_error)` replaces that with one warning:
`Telegram polling hiccup (NetworkError: …) - reconnecting automatically`. The callback must be a
plain function, because PTB rejects coroutines.

**Checks:**
- Backend: ruff, ruff format and strict mypy are clean; **110 tests pass** (was 65).
- New backend tests:
  - `test_actions.py`: 20 tests covering every transition, the bounce, expiry, a stale timer,
    a side flip between taps, the cap at the live size, trading off, and a partial rounding to 0.
  - `test_trading.py`: rounding, reply parsing, mainnet refusal, a non-testnet Exchange, SDK
    exceptions, warm-up failure, and the key not being kept.
  - Two-tap end-to-end and expiry through the real `/ws` in `test_app.py`, plus `order_log`
    round-trip and the one-line polling warning.
- Plugin: tsc is clean, **28 tests pass** (was 17), and `npm run build` works.

**Simulator check (2026-11-24 ~16:03 IST).**
- Setup: a throwaway backend on :8010 with **live testnet ETH prices**, a fake 0.5 ETH long
  (entry 1,700, liq 1,420, 5x) and a fake executor that "fills" after 1 s and shrinks the fake
  position. Vite ran on :5174 (`VITE_BACKEND_URL=http://127.0.0.1:8010`), and the simulator was
  driven through its automation API (`--automation-port 9898`). The scripts were kept out of the
  repo.
- The glasses showed, in order:
  1. HUD with `uPnL -$7.52 (-0.9%)  ROE -4.4%` (= -0.9 % × 5).
  2. The menu, with the cursor on `Close 50%` after a swipe down.
  3. Confirm: `SELL 0.25 ETH  reduce-only IOC` / `at ~1,685.0 mark, max 5% slippage` /
     `Tap again within 5s to SEND.`
  4. `SENDING`.
  5. `FILLED  Close 50%  ETH-PERP` / `Filled 0.25 @ 1684.9`.
  6. Back on the HUD after 8 s with `ETH-PERP LONG 0.25`.
  7. A second arm left alone for 6 s → menu with `Confirm expired - not sent`.
  8. A double-click → back to the HUD.
- The plugin console logged `ack tap -> confirm`, `ack tap -> sending`,
  `ORDER ETH close 50%: filled - Filled 0.25 @ 1684.9` and `ack back -> hud`.

**Side effect:** saving `backend/src` files reloaded the user's running `backend:dev` several
times. Telegram reconnected without being throttled each time; at the end, `/healthz` showed
`telegram: running @tripwire_g2_bot` and `trading: off` (no agent key).

**Not verified yet:**
- A real close on Hyperliquid testnet: it needs faucet funds, an authorized agent wallet, and an
  open position. Setup steps are in the README, "Trading on testnet".
- Real glasses.
- Whether HL's testnet accepts reduce-only partials below its $10 minimum order value. If not,
  the exchange's error message shows on the result screen.

---

## 2026-11-24 · Day 2 continued (no funded account needed)

The user's list, in the order built. The policy guard (4) and the adapter (7) come before 5, 3, 2
and 6 because every order path below goes through both. One commit per item.

### 13. WATCH mode: follow a public testnet address, read-only (item 1)

**What:** `WATCH_ADDRESS` in `.env`. If `HL_ACCOUNT_ADDRESS` is empty and `WATCH_ADDRESS` is
set, the backend streams that address's positions. The HUD header says `TRIPWIRE WATCH`, the
footer says `watch only  swipe: next  2x: exit`, and tapping does nothing (ack
`ok:false, note:"watch only"`). Trading is forced off (`DisabledExecutor("watch only: ...")`),
even if an agent key is set.

**Deviation: `clearinghouseState`, not `webData2`.** The request said "via public
info/webData2". Re-checked on 2026-11-24: testnet still rejects `webData2` (`Error parsing JSON
into valid websocket request`). `clearinghouseState {user}` carries the same positions, is public
for any address, and is what the feed already used for the own account. So WATCH is just "stream
someone else's address".

**Code:**
- `config.py`:
  - New fields: `watch_address` (validated and lowercased like `HL_ACCOUNT_ADDRESS`),
    `watch_mode` and `stream_address`. The own account wins if both are set.
- `state.py`:
  - `MarketState(account=..., watch=...)`, plus `mode` (`account` / `watch` / `price`) and
    `account_short` (`0x1234...abcd`).
- `hud.py`:
  - Frames gain `watch` and `address`.
- `actions.py`:
  - `handle(..., watch=)` refuses the menu.
- `main.py`:
  - The startup log line and `/healthz` → `hyperliquid.mode` / `address` say which mode is on.
- `hl/feed.py`:
  - **`MAX_CTX_SUBS = 25`**: at most 25 `activeAssetCtx` subscriptions, with the reference coin
    first. Some testnet addresses are market makers with 150+ positions; coins past the cap use
    mid as "mark" and the HUD says `(mid)`.
- Plugin `hud.ts`:
  - `headerLine(now, tag)`, `FOOTERS.watch`, and `Watching 0x1234...abcd: no position` when the
    watched address is flat. With the link down the WATCH tag is not shown.

**Finder script:** `npm run watch:find` (`backend/scripts/find_active_addresses.py`).
- It listens to the public `trades` stream (ETH, BTC, SOL; 20 s by default). Every trade carries
  `users: [buyer, seller]`. It then asks `/info clearinghouseState` which of those traders hold
  positions, and prints full addresses with the largest position and its liquidation distance.
  Addresses with a liquidation price and few positions are listed first.
- Options: `--seconds`, `--coins`, `--limit`. Read-only, no keys, and it calls `assert_testnet`.
- First run: 25 traders in 15 s, 6 with positions, e.g. `0xb7bd...6422` (7 positions).

**Checks:**
- Tests: **115 backend tests pass** (+5, new `test_watch.py`); plugin 29 (+1).
- Live: a throwaway app (port 8011, bot off) watched `0xb7bd...6422` on the real testnet feed.
  `/healthz` showed `mode: watch, positions: 7`, `trading: off - watch only`, and real positions
  with markPx, e.g. `BTC SHORT 0.03591 @ 59463.0 mark=59363.0 upnl=4.45 liq=164468.8`.
- The simulator check is in §20 (exit check).

### 14. Policy guard: one module every order passes through (item 4)

**Shape:**
- `orders.py` holds the order types:
  - `CloseOrder` (Day-2 `CloseRequest`, kept as an alias in `trading.py`)
  - `StopOrder` (move a stop-loss)
  - `OpenOrder` (open or add to a position)
  - `OrderResult`, with new statuses `blocked`, `placed` and `dry_run`
- `orders.py` also holds **`OrderService.submit()`, the only code path that sends**. It refreshes
  realized PnL, runs `PolicyGuard.check()`, and only then calls the exchange. A block returns
  `OrderResult(status="blocked", message=<reason>)` and nothing reaches the exchange.
- `policy.py`: `check_order(order, PolicyContext, PolicyLimits) -> Decision(allowed, rule,
  reason)`. It is **pure**, so every rule is tested without I/O. `PolicyGuard` wraps it with the
  live inputs.
- The action flow also calls `guard.check()` **at arm time** (`precheck`), so a blocked order
  shows `Blocked: <reason>` instead of a confirm screen. `submit()` checks again, because the
  position can change between the two taps.

**Rules (first failing rule wins; the rule names are stable):**

| Order | Allowed when | Refused with |
|---|---|---|
| any | — | `watch_only` (WATCH mode never trades) |
| Close | the position exists on the confirmed side. **Never blocked** by the kill switch or the loss lock: it only reduces risk | `no_position`, `side_mismatch`, `bad_size` |
| Stop | the new SL is on the protective side of mark **and** tighter than the current one (or there was none). Not blocked by the kill switch or the loss lock | `sl_would_trigger` (at or past mark: it would fire at once, use Close), `sl_loosen`, `no_price`, `bad_price` |
| Open | none of the refusals below apply | in this order: `kill_switch`, `daily_loss_lock`, `sl_required`, `bad_size`, `max_notional` ($250), `max_leverage` (5x), `no_price`, `sl_wrong_side`, `tp_wrong_side`, `opposite_position` (would flip: use Close) |

Notes on the limits:
- They are inclusive: $250 at 5x is allowed.
- The notional cap is **per order**. Adding to an existing same-side position is allowed if that
  order is within limits.

**Kill switch (`KillSwitch`):**
- It is ON if `KILL_SWITCH=true` in `.env` **or** it was engaged with `/kill` in Telegram.
- The Telegram flag is stored in a new SQLite `kv` table, so it survives restarts.
- `/unkill` only clears the Telegram flag. With the `.env` flag set it replies that `.env` keeps
  it on.
- `/healthz` → `policy` shows `kill_switch`, its `kill_switch_source`, `realized_pnl_today`,
  `loss_locked` and the limits.

**Telegram owner (`TELEGRAM_OWNER_ID`):**
- New: anyone can message a public bot, so `/kill`, `/unkill` (and later `/status`, forwarded
  drafts and alert DMs) only work for this numeric user id. Everyone else gets
  "Not authorized.", and with no owner set, nobody is authorized.
- `/start` now tells the sender their id and how to set it.

**Daily loss lock:** `PolicyGuard(pnl_source=...)` sums realized PnL for today (UTC). The
source is plugged in with the exchange adapter (§15). If the source fails, the guard keeps the
last value instead of failing open to 0.

**Checks:** **155 backend tests pass** (+40, new `test_policy.py`). Every rule has a refusal
test with its exact reason, plus tests that:
- closes go through with the kill switch on;
- the kill switch beats everything for opens;
- the kill switch persists in SQLite;
- the guard keeps the last PnL on error;
- `OrderService` never calls the exchange when blocked;
- `/kill` and `/unkill` are owner-only.

Plugin: result titles `BLOCKED`, `PLACED` and `DRY RUN` (29 tests). mypy, ruff and tsc are clean.

### 15. Exchange-agnostic adapter (item 7): Hyperliquid, fake, Bybit-demo stub

**Interface (`exchanges/base.py`, `ExchangeAdapter` Protocol, `runtime_checkable`):**
- The four the brief asked for: `stream_positions()`, `close_reduce_only()`, `set_sl()`,
  `place_order()`.
- Helpers the screens and the guard need: `current_stop(coin)`, `realized_pnl_today()`,
  `size_decimals(coin)`, `start()`.
- Flags: `name`, `status`, `detail`, `can_trade`, `dry_run`.
- `positions_stream(state)` turns MarketState changes into an async iterator. Adapters that keep
  positions in MarketState use it: Hyperliquid via `hl/feed.py`, and the fake.
- **Only `OrderService.submit()` calls the order methods** (via `ws.send_order`, which picks the
  method by `order.kind`).

**Hyperliquid (`exchanges/hyperliquid.py`), which replaces Day-2's `HyperliquidExecutor`:**
- It no longer calls `Exchange.market_close()`. It builds actions from the SDK's own pieces
  (`order_request_to_order_wire`, `order_wires_to_order_action`), signs with
  `sign_l1_action(..., is_mainnet=False)`, and POSTs the same payload shape as
  `Exchange._post_action`.
- **Why:** this gives one build-and-sign path that DRY_RUN (§16) can run end to end, skipping
  only the POST.
- Consequence: the side and size come from **our** MarketState (clearinghouseState, ≤ about 5 s
  old) instead of a fresh `user_state` call. `reduce_only` still guarantees a close can't flip.
- `assert_testnet` runs in the constructor and before every signature. A test spies on
  `sign_l1_action` and asserts `mainnet=False`.

Order shapes:
- `close_reduce_only`: IOC limit at mid ± 5 %. The price is rounded like the SDK does it
  (`round_px`: 5 significant figures, at most 6 − szDecimals decimals) and the size is rounded
  down. 100 % means the live size.
- `set_sl`: a trigger `{isMarket: true, tpsl: "sl"}`, reduce-only, sized to the position, with
  grouping `positionTpsl`. **The new stop is placed first, and the old one is cancelled only
  after that** (`{"type":"cancel","cancels":[{"a":asset,"o":oid}]}`).
  - Why that order: the brief said "cancel the existing SL and place a new one", but
    cancel-first would leave the position unprotected if the new stop were rejected.
  - Briefly there are two reduce-only stops, and the tighter one fires first.
  - If the cancel fails, the result says `old SL NOT cancelled: <reason>`.
- `place_order`: `updateLeverage` (cross, integer), then one `normalTpsl` action with the entry
  (GTC limit at entry, or IOC at mid ± 5 % for market) plus the SL trigger (plus TP).
  Size = notional / price, rounded down.
- `current_stop`: `/info frontendOpenOrders`, filtered to reduce-only `Stop*` triggers on the
  coin. It picks the tightest: the highest for a long, the lowest for a short.
- `realized_pnl_today`: `/info userFillsByTime` since 00:00 UTC, as Σ closedPnl − Σ fee. This
  is the policy guard's `pnl_source`.
- Replies: `parse_order_response` now also accepts the string statuses Hyperliquid uses for
  trigger and TP/SL children and cancels (`waitingForTrigger`, `waitingForFill`, `success`) →
  `placed`.

**Not verified live (no funded account yet):**
- `positionTpsl` sizing: we send the position size. A low-confidence community source says the
  web UI sends `0` with `isPositionTpsl`. If testnet rejects ours, `set_sl` returns the
  exchange's reason, and the old stop stays because we place first.
- The trigger `limit_px` = trigger price, following the SDK's `basic_tpsl` example.

**Fake (`exchanges/fake.py`):** in-memory Hyperliquid-shaped rows pushed into MarketState. Closes
fill at mid, there is one stop per coin (place, then the old one is dropped), and realized PnL is
tracked. Used by the tests and by the demo backend (§20). This replaces the ad-hoc fakes from
Day 2.

**Bybit demo (`exchanges/bybit_demo.py`):** a stub that satisfies the Protocol. It accepts only
`https://api-demo.bybit.com` (exact match, like `assert_testnet`). Every order returns "stub - not
implemented". The docstring maps each method to its Bybit v5 endpoint (e.g.
`/v5/position/trading-stop` sets the stop in one call, with no cancel step).

**Wiring:**
- `create_app(make_adapter=...)` takes a factory (tests and demo pass `FakeAdapter`). The default
  is `HyperliquidAdapter`.
- The price feed stays in `main.py` and also serves price-only and WATCH mode.
- New `ClientHub` in `ws.py` counts glasses clients. `/healthz → ws_clients` used to count every
  MarketState listener, and the positions stream adds one.
- `/healthz → trading` now has `exchange`, `enabled` (= `can_trade`) and `dry_run`. `hello` and
  `hud` frames carry the same.

**Checks:**
- **163 backend tests pass**; mypy and ruff are clean.
- New `test_trading.py` checks the exact wire for each order type:
  - Close: `{"a":4,"b":false,"p":"3319.7","s":"0.25","r":true,"t":{"limit":{"tif":"Ioc"}}}`.
  - SL: place, then cancel; and the old stop is kept if the new one is rejected.
  - Open: leverage, then entry with SL and TP.
  - Plus: testnet signing, the key not being kept, a non-testnet Info refused, `current_stop`
    picking the tightest, PnL arithmetic, the fake, and the Bybit stub.
- Socket tests now run on `FakeAdapter`.

**Fix, noted for honesty:** the §14 commit corrupted one character in `test_app.py`'s docstring
(`—` became `â€”`). Windows PowerShell 5.1 `Get-Content`/`Set-Content` re-encoded the UTF-8
file. It's fixed now, and file edits no longer go through PowerShell.

### 16. DRY_RUN: real testnet payloads, really signed, never sent (item 5)

**`DRY_RUN=true` is the default.** `HyperliquidAdapter(dry_run=True)` runs the **same**
build-and-sign path as live trading (§15). In `_submit()`, after `assert_testnet` and
`sign_l1_action(..., is_mainnet=False)`, it writes the exact `/exchange` payload to the log and
to `backend/data/dry_run_orders.jsonl` (gitignored), then returns `DRY_RUN_REPLY` instead of
POSTing.

**Signer:**
- If `HL_AGENT_KEY` is set, the payload is signed with it but still not sent.
- Otherwise it's signed with a **throwaway key** from `eth_account.Account.create()` at startup.
  The key is never logged or stored; only its public address appears, as `signer`.
- `/healthz → trading.detail` says `DRY RUN: signed with throwaway key 0x1234...abcd, NOT sent`.
  Live mode says `LIVE on testnet: agent key ...`.

**When trading is enabled:**
- `can_trade` = signer present, not WATCH, and (account set **or** dry run).
- So dry run can sign opening drafts even in price-only mode (item 6). There are no positions
  then, so the close menu can't open.
- Live trading still needs `HL_ACCOUNT_ADDRESS` + `HL_AGENT_KEY` + `DRY_RUN=false`.
- WATCH never trades, dry run or not.

**What still talks to Hyperliquid in dry run:** only the public `/info` meta (asset ids,
szDecimals), so the payloads use the real asset index, lot rounding and tick rounding. Nothing is
POSTed to `/exchange`.

**Results:**
- New status `dry_run` (result title `DRY RUN`), with messages saying what was signed:
  - `DRY RUN: sell 0.25 ETH IOC @ 3319.7 signed, not sent`
  - `DRY RUN: SL 3412 signed + cancel of old SL, not sent`
- Rows go to `order_log` like any other result.

**On the glasses:**
- The header shows `TRIPWIRE DRY RUN` whenever trading is enabled in dry-run mode (`DEMO` for the
  fake exchange, `WATCH` for watch mode).
- The confirm line reads `Tap again within 5s to SIGN (dry run).`

**Testnet guard:** the constructor refuses a non-testnet URL even in dry run, and every signature
call is preceded by `assert_testnet`.

**Checks:** **170 backend tests pass** (+7, new `test_dry_run.py`):
- The dry-run close wire is byte-identical to the live wire from `test_trading.py`.
- `recover_agent_or_user_from_l1_action` **recovers the signer from the logged payload with
  `is_mainnet=False`** (and does *not* with `True`). This proves a real testnet signature.
- The agent key never appears in the log.
- Agent-key signing; SL = place + cancel, both signed; an open without any account = leverage +
  order legs; WATCH never trades; mainnet refused.

Test settings now pass `dry_run=False` by default, so app tests never load metadata over the
network. Plugin: 30 tests.

### 17. Move stop-loss: "SL to breakeven" and "SL tighter 1%" (item 3)

**Flow (`actions.py`, rewritten to be generic):**
- Menus have a `kind` (`MENUS`). The **actions** menu is now Close 25% / 50% / 100% /
  **Move SL...** / Back.
- **Move SL...** opens the **sl** submenu: SL to breakeven / SL tighter 1% / Back.
- An armed order is any `Order` (`Armed.order`) plus a `view` dict for the confirm screen, so
  closes, stop moves and (in §19) opening drafts all share the one two-tap confirm.
- "Back" in the submenu returns to its parent menu. Swiping on the confirm screen cancels back to
  the menu it was armed from, and a double tap returns to the HUD.

**The current stop-loss is fetched from the exchange when the submenu opens:**
- `Outcome.fetch_stop` triggers `ws.load_stop()` → `adapter.current_stop(coin)` →
  `flow.set_stop()`, and the screen shows `SL now 3,300  entry 3,412  mark 3,494`.
- Until it arrives the screen says `SL now loading...`, and picking an item answers "loading the
  current stop-loss - tap again".
- If the lookup fails, the submenu stays "not loaded" with a note. It never pretends there is no
  stop.

**Maths (then validated by the policy guard at arm time and again at send):**
- **SL to breakeven:** new trigger = entry price.
- **SL tighter 1%:** new trigger = current SL moved 1 % *of mark* toward the price (long:
  `sl + 0.01 × mark`, short: `sl − 0.01 × mark`). With no current SL it's refused ("No stop-loss
  to tighten - use SL to breakeven").
- The trigger is rounded with `round_px` (Hyperliquid tick rules) when szDecimals is known.

Cases the guard catches, with the reason shown in place of a confirm screen:
- Breakeven on a **losing** long: the entry is above the mark, so it would fire at once →
  `Blocked: SL 1,700 is above mark 1,650 - it would fire at once`.
- Breakeven when the stop is already past entry → `not tighter ... loosening is refused`.
- Tighten 1 % past the mark → `sl_would_trigger`.

**Sending:**
- `StopOrder` goes through `OrderService.submit` → `adapter.set_sl`. The new stop is placed
  first and the old one cancelled afterwards (§15).
- On the fake exchange the result is `SL set at 3412; old SL cancelled`. In dry run it's
  `DRY RUN: SL 3412 signed + cancel of old SL, not sent`.

**Protocol and audit:**
- Confirm screens carry `action` (`close` / `stop`). Stop screens show `old_sl`, `new_sl`,
  `mark`, `entry`, `size`. The SL menu carries `kind: "sl"`, `sl_loaded` and `current_sl`.
- The `order` frame has `kind`; `fraction` is null for stop moves.
- `order_log` has a new `kind` column. `Store.init()` migrates Day-2 databases with
  `ALTER TABLE ... ADD COLUMN kind ... DEFAULT 'close'` (tested on a Day-2 schema).

**Plugin:**
- `MOVE SL  ETH-PERP LONG 0.50` + `SL now ...` + items.
- Confirm: `SL 3,300 -> 3,412  reduce-only` / `new SL placed first, then old cancelled` /
  `mark 3,494.4  entry 3,412`.
- `OrderScreen` is a union (`CloseScreen | StopScreen`); a missing `action` means a close (v2.0
  frames).

**Checks:**
- **181 backend tests pass** (+11, new `test_stop_loss.py`):
  - The submenu waits for the stop.
  - Breakeven long in profit → the exact `StopOrder`.
  - Breakeven while losing → blocked with the reason.
  - Tighten long (1600 → 1617.5) and short (1900 → 1882.5).
  - Tighten without a stop / past the mark; loosening blocked.
  - Back / cancel navigation; a side flip between taps blocks the send.
  - **SL to breakeven end to end through `/ws` on the fake exchange** (3300 → 3412, old stop
    cancelled, `order_log.kind = stop`).
  - The DB migration.
- Plugin: 32 tests (+2).

### 18. Liquidation alerts: risk engine, glasses RISK screen, Telegram DM fallback (item 2)

**Engine (`risk.py`, `RiskEngine`, pure).** It runs per position (coin **and** side), on
`liq_dist_pct`, which is measured from markPx. A position whose mark is still the mid fallback
is skipped, because liquidation runs on mark.

| Event | Rule |
|---|---|
| fire | distance ≤ `ALERT_LIQ_PCT` (5 %) **and** armed **and** not snoozed |
| re-arm | distance ≥ threshold + `ALERT_HYSTERESIS_PCT` (1 %). A price wobbling 4.9 ↔ 5.2 % alerts **once** (tested) |
| snooze | `ALERT_SNOOZE_S` = 300 s. Afterwards it fires again if still inside |
| threshold change | re-arms everything, so a position already inside the new threshold alerts at once |
| position closed | its state is forgotten |

**Service (`AlertService`):**
- It consumes **`adapter.stream_positions()`**, the item-7 interface, used for real here. It
  re-evaluates on every update and at least every `TICK_S` = 5 s (for snooze expiry without a
  price move).
- Delivery: if glasses are connected, `ClientHub.raise_alert` puts the RISK screen on every
  client. If **none are connected, it sends a Telegram DM to `TELEGRAM_OWNER_ID`**
  (`TelegramBot.send_owner`). With no bot or no owner it logs "nowhere".
- `/healthz → alerts` shows the threshold, hysteresis, snooze, `fired`, positions `inside` the
  threshold, `snoozed` positions, and the `last` delivery with its target.

**Threshold at runtime:** the new owner-only `/threshold` command (`/threshold 8`) is stored in
the SQLite `kv` table and reloaded at startup. The exit check's "lowering the threshold" is this
command, or `ALERT_LIQ_PCT`. A higher percentage alerts earlier.

**Glasses (`ActionFlow.raise_alert`, menu kind `alert`):**
- Header `▲ RISK  ETH-PERP LONG 0.50`, then `Liq 2,980  3.9% from mark (<5%)`, then
  **Move SL / Close 50% / Close all / Snooze 5m**.
- These reuse the existing machinery:
  - Move SL opens the §17 SL submenu, and its Back returns to the alert.
  - Close 50% / Close all arm the same two-tap confirm. A swipe-cancel returns to the alert.
  - Snooze → `Outcome.snooze` → `risk.snooze()` → HUD, with ack "alert snoozed 5 min".
- **An alert never hijacks a confirm or an order in flight.** It waits (`pending_alert`) and
  appears as soon as that screen is left.
- In WATCH mode the alert still shows (informational). Order items answer "Watch only - not your
  account, nothing sent"; Snooze works.

**Glyph:** the glasses text has been ASCII-only since Day 1. `▲` (U+25B2, `RISK_GLYPH` in
`hud.ts`) is now the single allowed exception, because the brief asked for it. `fit()` still
turns every other non-ASCII character into `?`. Whether it renders is checked in the simulator in
§20; if it doesn't, the constant becomes `/!\`.

**Telegram DM text:** `▲ RISK  ETH-PERP LONG 0.5` / `Liquidation 1,420 is 4.2% from mark 1,482
(alert at 5%).` / "Glasses not connected - open Tripwire to act (Move SL / Close), or send
/status."

**Checks:** **199 backend tests pass** (+18, new `test_alerts.py`):
- Engine: fire once, hysteresis re-arm, wobble = one alert, snooze, mark-only, threshold
  re-arm, per coin+side.
- Delivery: glasses vs DM vs nowhere; the DM text; threshold persistence; the run loop reacts
  to stream updates.
- Flow: alert menu, waits for a confirm, snooze, alert → confirm → cancel back, Move SL → Back
  to the alert, WATCH informational.
- **End to end** via `/ws`:
  - Moving markPx to 3100 (liq 2980 = 3.9 %) puts the alert on the connected client, and Snooze
    works.
  - **With no client, the same move produces a DM** (fake notifier).

Plugin: 33 tests (RISK screen; `fit` keeps only `▲`).

### 19. Telegram depth, part 1: forward-to-bot order drafts (item 6)

**Change of plan (user, 2026-11-24): no Anthropic key.** Drafts come from a **deterministic
parser** (`drafts.py`, regex tokenizer, no LLM). Reply drafts (§20) use OpenRouter with template
fallback.

**Parser (`parse_draft` → `OrderDraft`):** the strict schema is `coin, side, notional_usd,
leverage, entry_px (None = market), sl_px, tp_px`, plus `defaults`.

What it reads:
- `BTC long 96.5k sl 95k tp 99k` and `ETH short @3400 SL 3480 TP 3200`.
- Suffixes `k`/`K`/`m`, thousands commas (`96,500`), `.055`.
- Side words: `buy`/`sell`/`long`/`short`.
- Field labels: `stop`/`stop-loss`/`target`/`take-profit`/`entry`/`at`/`@`.
- `market`/`mkt`.
- Leverage as `5x` / `5 x` / `lev 5`. Notional as `$200`, `200$`, `200 usd(c)`, `size 200`.
- `BTC-PERP` / `BTCUSDT` / `BTC/USD` are all read as BTC.
- The first unlabelled number after the coin/side is the entry.
- Missing notional or leverage → `DRAFT_DEFAULT_NOTIONAL_USD` ($100) and
  `DRAFT_DEFAULT_LEVERAGE` (1x), listed in `defaults`. The glasses mark them with `*`.

**Ambiguous or malformed → `DraftError` with a reason** (the Telegram reply is
`Not an order I can draft: <reason>.`):
- no coin, or more than one coin; no side, or both sides;
- an unlabelled extra number (`which price is 95k? label it (sl / tp / @entry)`);
- the same field twice with different values; `market` plus a bare price;
- a label without a number; fractional leverage; zero or negative values;
- an unknown coin (once the coin list is known); more than 300 characters.

Choices:
- With the coin list loaded (allMids perps, at least 20), other words are ignored as chatter
  (`lfg BTC long ... pls`). Without it, they count as coins, so the message is rejected rather
  than guessed.
- A missing SL is **not** a parse error: the policy guard refuses it with its own reason. SL/TP
  on the wrong side is left to the guard too, so its reasons stay in one place.

**Bug found by the tests and fixed:** `tp 99k $1000` was read as notional $99,000 plus tp 1000.
The suffix form `200$` allowed a space before `$`. A trailing `$` must now touch its number. A
regression test covers it.

**Telegram (`make_draft_handler`).** This **replaces the Day-1 forward echo.**
- It handles private text or captions that aren't commands, forwarded or typed.
- Owner only (`TELEGRAM_OWNER_ID`); anyone else is told it's owner-only.
- The text is parsed, `check_draft` runs `PolicyGuard.check(draft.to_order(), position, mark)`,
  and `push_draft` sends it to the glasses.
- Reply:
  ```
  FORWARDED - your instruction, not advice
  BTC LONG $100 1x @ 96,500  SL 95,000  TP 99,000
  Defaults used: notional, leverage
  Policy: OK - within limits, has a stop-loss        (or: BLOCKED by policy: Notional $1,000 > max $250)
  On your glasses now: two taps to confirm.          (or: No glasses connected: it will show when they connect (5 min).)
  ```
- Only metadata is logged (chat id, message id, forwarded flag, length), never the text.

**Glasses (`ActionFlow.raise_draft`, menu kind `draft`):**
- Screen: `FORWARDED - your instruction, not advice` / `BTC LONG $100* 1x*  @96,500` /
  `SL 95,000  TP 99,000` / `Policy OK` (or `BLOCKED: <reason>`).
- Items: **Place order / Discard**, or **only Discard** when blocked.
- Place order builds the `OpenOrder` and runs a fresh policy pre-check (e.g. a `/kill` sent since
  the draft blocks it here). Then comes the same two-tap confirm:
  `BUY ~0.001036 BTC  $100 1x  @96,500` / `SL 95,000  TP 99,000  attached` / `your forwarded
  instruction, not advice`.
- After that: `OrderService.submit` → `adapter.place_order` (§15: leverage, then entry + SL + TP
  as `normalTpsl`).
- Like alerts, a draft waits during a confirm or an order in flight.
- If no glasses are connected, `ClientHub` keeps the latest draft for `PENDING_DRAFT_S` = 300 s
  and shows it on the next connect.
- The reference price for market drafts and the guard falls back position mark → markPx → mid
  (`ws.ref_price`).

**Checks:**
- **248 backend tests pass**:
  - `test_drafts.py`, 40: 13 formats, defaults/leverage/notional forms, chatter, summary, number
    parsing, 18 refusal cases with their exact reasons, plus the regression.
  - `test_forward_drafts.py`, 11: the flow; the handler replies for OK / blocked / ambiguous /
    stranger; **end to end**: forward → glasses draft → Place → two taps → `place_order` on the
    fake; a 10x draft shows `BLOCKED: Leverage 10x > max 5x` with only Discard; a draft forwarded
    with no glasses appears on the next connect.
- Plugin: 35 tests.

### 20. Telegram depth, part 2: inbox HUD line, reply after a fill, /status (item 6)

**Inbox (`inbox.py`), `TG 1 new @name: <first 30 chars>` on the HUD:**
- A business message from someone other than the owner is reduced to its **first 30
  characters**. That preview is kept **in memory only**, for 10 minutes (`WINDOW_S`). The rest
  of the text is dropped when it arrives (a test checks the full text isn't kept). Nothing is
  logged or written to SQLite, so the privacy rule still holds.
- The owner's own messages and edits don't count.
- The name is `@username`, else the first name. Non-ASCII becomes `?` on the glasses.
- `hud` frames gain `inbox: {count, from, preview}` (or null). A new message wakes every client
  (`ClientHub.wake_all`).
- The inbox also remembers the **reply target** (business connection id, chat id, name; no
  text) for 30 minutes.
- **Needs Telegram Premium** to receive business messages for real (§11). Tested with fake
  updates.

**Reply after a fill (`replies.py`, flow mode `reply`):**
- After a **real** change (`filled` / `placed`; never `dry_run` or `blocked`), and only if a
  business chat wrote in the last 30 minutes, a draft is made and offered as soon as it's ready.
  It replaces the result screen **only if the user is still looking at that result** (token
  check).
- Screen: `REPLY to @alice on Telegram?` / `"Trimmed half of my ETH."` /
  `drafted from a template`. Footer `tap: send reply  2x: skip`. Swipes do nothing; it skips
  itself after `REPLY_HOLD_S` = 20 s.
- Tap → `TelegramBot.send_business_reply(connection_id, chat_id, text)` =
  `send_message(..., business_connection_id=...)`, which answers *as the user* and needs the
  "reply" right. The plugin gets a `{"type":"reply","ok":..,"to":..}` frame.

How the draft text is made:
- **Templates** (≤ 60 characters): `Trimmed half of my ETH.`, `Trimmed a quarter of my ETH.`,
  `Closed my ETH long.`, `Moved my ETH stop to 1,700.`, `Opened a small BTC long.`, or
  `..., SL moved to {sl}.` when both happened.
- **OpenRouter** (if `OPENROUTER_API_KEY` is set): `POST https://openrouter.ai/api/v1/chat/completions`,
  model `OPENROUTER_MODEL`. The default is `google/gemma-4-31b-it:free`, picked from the live
  `/api/v1/models` list on 2026-11-24 (12 `:free` models existed); free models come and go.
  - Timeout 8 s, `max_tokens` 40. The answer is cleaned (first line, no quotes) and must be
    1–60 characters.
  - **Any problem falls back to the template**: no key, 429 rate limit, 5xx, timeout, empty or
    too long.
- **Privacy:** OpenRouter gets only trade facts (`I just closed half of my ETH long at about
  1,684.9.`), never chat text, names or ids. A test asserts the exact request body.
- An `sk-or-...` key is redacted from logs like the bot token.

**`/status` (owner only):**
- Mode (own / WATCH / price only); trading (`LIVE on testnet` / `DRY RUN` / `DEMO` / `off
  (reason)`); kill switch; alert threshold.
- Then up to 20 positions (`ETH-PERP LONG 0.5 @ 3,412  mark 3,494.4  uPnL +41.20 (+2.4%)  liq
  2,980 (14.7%)`) and the total uPnL.
- Caught by a test: the fake exchange first showed as `LIVE`. It now says
  `DEMO (in-memory fake exchange)`.

**Checks:**
- **268 backend tests pass** (+20, new `test_telegram_depth.py`):
  - Inbox: 30 characters, window, reply target, own and edited messages ignored, name fallback.
  - Six templates; cleaning.
  - The OpenRouter request (URL, auth, model, **facts-only body**); fallback on 429 / 500 / too
    long / empty; no request without a key.
  - Reply screen: tap sends, 2x skips, a stale offer is dropped.
  - `/status`: owner-only, and the exact text.
  - **End to end:** inbox line on the HUD → Close 50% → filled → reply offer → tap → sent with
    the right connection and chat.
- Plugin: 37 tests (TG line, reply screen, 2x = back on the reply screen).

### 21. Exit check in the simulator (2026-11-24, ~17:28–17:33 IST)

**New tool for it: `npm run backend:demo`** (`backend/scripts/demo.py`).
- Port 8010. It uses the real `.env` (pairing code, bot token, owner id).
- The **fake exchange** holds a 0.5 ETH long (entry 1,650, liq 1,420, stop 1,500) on **live
  testnet prices**. `--watch 0x...` uses WATCH mode instead.
- It **never starts Telegram polling**: a second poller with the same token would fight the
  real backend (HTTP 409).
- Alert "DMs" are sent with a plain `sendMessage` when `TELEGRAM_OWNER_ID` is set. Otherwise
  they're recorded at `GET /demo/dms`.
- Localhost-only routes inject a forwarded message (through the **real** bot draft handler), a
  threshold change, or a business message.
- Vite ran on :5174 (`VITE_BACKEND_URL=http://127.0.0.1:8010`), and the simulator was driven
  through its automation API.
- Screenshots: `docs/screenshots/day2/`.
- First run bug, fixed: `Settings.model_copy(update=...)` doesn't validate, so `db_path` stayed a
  string. The demo now builds `Settings(**{**dumped, **overrides})`.

| Exit-check item | Result | Screenshot |
|---|---|---|
| WATCH mode shows a real testnet position live | ✅ Watched `0x7149...7bd0` (found by `watch:find`): `TRIPWIRE WATCH` / `BTC-PERP LONG 0.2325 @ 59,658  10x` / `uPnL +$66.05 (+0.5%)  ROE +4.8%` / `Liq 49,860  16.8% away`. 12 s later uPnL read `+$61.52`. A tap was refused: `ack tap -> hud (watch only)` | 01, 02 |
| Lowering the bar fires an alert on the glasses | ✅ At the default 5 % nothing fired (16.8 % away). `POST /demo/threshold 20` (= `/threshold 20`) put **`▲ RISK  BTC-PERP LONG 0.2325` / `Liq 49,860  16.8% from mark (<20%)`** / Move SL / Close 50% / Close all / Snooze 5m on the glasses. `/healthz → alerts.last.delivered_to = "glasses (1)"`. **The `▲` glyph renders in the simulator** | 03 |
| …and with the plugin closed, a Telegram DM | ✅ in code ⚠️ not delivered to a phone. With the simulator closed (`ws_clients=0`), `/threshold 19` re-armed it and the alert went to the Telegram notifier with the full DM text (`▲ RISK  BTC-PERP LONG 0.23249` / `Liquidation 49,860.4 is 16.8% from mark 59,943 (alert at 19%).` / …). **`TELEGRAM_OWNER_ID` is not set in `.env`**, so it was recorded (`delivered_to_telegram: false`), not sent. Set the owner id (from `/start`) to receive it | — |
| "SL to breakeven" through the confirm, against the fake exchange | ✅ Tap → Move SL... → `MOVE SL  ETH-PERP LONG 0.50` / `SL now 1,500  entry 1,650  mark 1,702` → SL to breakeven → `CONFIRM  SL to breakeven` / `SL 1,500 -> 1,650  reduce-only` / `new SL placed first, then old cancelled` → tap → `PLACED  SL set at 1650; old SL cancelled` | 05, 06, 07 |
| Forwarding `BTC long 96.5k sl 95k tp 99k` shows the draft screen | ✅ Bot reply: `FORWARDED - your instruction, not advice` / `BTC LONG $100 1x @ 96,500  SL 95,000  TP 99,000` / `Defaults used: notional, leverage` / `Policy: OK - within limits, has a stop-loss` / `On your glasses now: two taps to confirm.` Glasses: the same, with Place order / Discard. Place → `CONFIRM  Place order  BTC-PERP LONG` / `BUY ~0.001036 BTC  $100 1x  @96,500` / `SL 95,000  TP 99,000  attached` / `your forwarded instruction, not advice` | 08, 09 |
| An over-limit draft is blocked by the policy guard with a clear reason | ✅ `BTC long 96.5k sl 95k tp 99k $1000` → bot: `BLOCKED by policy: Notional $1,000 > max $250`; glasses: `BLOCKED: Notional $1,000 > max $250` with **only** Discard | 10 |
| (bonus) business message line | ✅ `TG 1 new @alice: still in that ETH long or ou~` on the HUD (the 30-character preview plus the name hits 46 columns, so `fit` ends it with `~`) | 04 |

**UX bug found by the exit check and fixed:**
- After the SL move, the **reply offer replaced the `PLACED` result almost instantly**, because
  template drafts take no time. The user could miss the confirmation.
- Fix: the reply screen keeps the result as its first line (`PLACED  SL set at 1650; old SL
  cancelled` / `REPLY to @alice on Telegram?` / `"Moved my ETH stop to 1,650."` /
  `drafted from a template`). `ui` gains `result_status` and `result_message`.
- Re-checked in the simulator (screenshot 07); tests updated.

**What was NOT verified, and why:**
- A real DM to a phone: no `TELEGRAM_OWNER_ID`.
- A real Telegram forward: the exit check injected text through the same handler. Forwarding
  from the phone needs the owner id and the real backend (the demo doesn't poll).
- Business messages and replies on real Telegram: Premium.
- Any order on Hyperliquid: no funded account. Dry run is proven by signature recovery (§16),
  the fake exchange covers the flows, and `positionTpsl` sizing stays the first thing to verify
  live (§15).
- Real glasses.

**Housekeeping in this commit:**
- `httpx` and `eth-account` are now **declared** backend dependencies (imported directly since
  §15/§20; they were transitive before; `uv.lock` updated).
- The outdated pyproject comment about the SDK's WebsocketManager now says the SDK is pinned for
  the signing wire format.
- README (features, "Try it without an account", Telegram commands, dry run, safety rules 3/4/7,
  privacy, troubleshooting), `docs/ws-protocol.md` (v2 additions) and `docs/architecture.md`
  (modules, the one order path, next steps) updated.

Final state: **268 backend tests**, **37 plugin tests**; ruff, mypy strict and tsc clean.

### 22. Real Telegram check by the user (2026-11-24, ~23:35–23:42 IST)

**Setup:** the user added `TELEGRAM_OWNER_ID` (their numeric id, from the bot's `/start` reply)
to `.env` and restarted `backend:dev`.

**Problem: the bot still treated them as a stranger.**
- Cause: **two backends were polling with the same token.** The `backend:dev` started at 13:28
  kept running after its terminal tab was closed (Ctrl+C never reached it). It had never read
  the new `.env`, and Telegram handed `/start` to it.
- The new backend logged
  `Conflict: terminated by other getUpdates request; make sure that only one bot instance is running`
  every few seconds, as a one-line warning thanks to §12's `error_callback`.
- Found by listing `uvicorn` processes with their start times: one tree from 13:28 and one from
  23:34.
- The user stopped the old tree with `taskkill /PID <pid> /T /F`. The assistant's own attempt
  was blocked by the permission check, which is correct: it's the user's process.
- The Conflict lines stopped by themselves within about 30 s, with no restart of the new backend.
- The fix is in the README troubleshooting table.

**Verified by the user on real Telegram:**
- `/start` → "You are the owner".
- `/status`.
- Two typed drafts reached the handler (log `draft candidate ... forwarded=False`; the text is
  never logged).
- `/kill` → `KILL SWITCH engaged (Telegram)` → `/unkill` → `Kill switch released via Telegram`.
- `/threshold`.
- The user reports all of them working.
- End state from `/healthz`: kill switch off, threshold 5 %, price-only mode.

**Still not verified on real Telegram:**
- **The alert DM**: no alert fired (`alerts.fired = 0`), because price-only mode has no
  positions. It needs `WATCH_ADDRESS` (or an own account), the glasses closed, and
  `/threshold` above a position's liquidation distance.
- A *forwarded* (not typed) message: same handler, filter `TEXT | CAPTION`.
- Business inbox and replies: need Premium.

---

## 2026-11-25 · Day 3

Rules carried over: BUILD_LOG on every change, a commit per milestone, ask before pushing, no
secrets, testnet only, reduce-only by default, two-tap confirm, no advice. Testnet funds are
pending, so `DRY_RUN=true` and WATCH mode remain the real-data sources. Item 0 (push `c4c4b27`)
was already done at the end of Day 2; `origin/main` contains it.

### 23. Go-live readiness: `golive:check`, GO_LIVE.md, stop-loss payload audit (item 1)

**Payload audit against hyperliquid-python-sdk's own examples** (`examples/basic_tpsl.py`,
`examples/rounding.py`, fetched from GitHub; they aren't in the installed package). Two fixes and
one open point:

1. **Bug: a stop-loss's limit price was equal to its trigger.**
   - The SDK's example gives a market stop a limit *past* the trigger (sell stop: trigger 2600,
     limit 2500).
   - With limit = trigger, a price that gaps through the stop can't fill it, so the stop fails
     exactly when it's needed.
   - Fix: `trigger_limit_px()` = trigger −10 % for sells, +10 % for buys (the Hyperliquid UI's
     TP/SL market slippage, `TRIGGER_SLIPPAGE`), rounded to a valid tick.
   - Used for `set_sl` and for the SL and TP legs of `place_order`. Example:
     `{"t":{"trigger":{"triggerPx":"3412",...}}, "p":"3070.8"}`.
2. **Rounding above 100,000.** `rounding.py` says any integer price is valid above 100k. Our
   5-significant-figure rounding turned BTC 105,432.7 into 105,430. `round_px` now rounds to an
   integer above 100k.
3. **Bug found by a new property test:** a price below the smallest tick (0.004 with
   szDecimals 5 → 1 decimal) rounded to **0.0**. No real asset has that combination, but
   `round_px` now raises instead of ever producing a zero price.
4. **Open point:** `positionTpsl` isn't covered by the SDK examples. We still send the position
   size, not `0`. It's step 6 of `docs/GO_LIVE.md`, the first thing to watch live, with the
   one-line change if it's rejected.

New helpers in `trading.py`: `is_valid_px`, `is_valid_sz`, `trigger_limit_px`, `TRIGGER_SLIPPAGE`.
Lot sizes come from testnet meta (2026-11-25): BTC 5, ETH 4, SOL 2, DOGE 0, out of 213 perps.

**`npm run golive:check`** (`scripts/golive_check.py` → `src/tripwire/golive.py`), in order:

| # | Check | How |
|---|---|---|
| 1 | `HL_ACCOUNT_ADDRESS` + `HL_AGENT_KEY` present | format validated by `Settings` |
| 2 | agent authorized for the account | `/info userRole(agent)` → `{"role":"agent","data":{"user":<account>}}`, else `extraAgents(account)` with an unexpired `validUntil` |
| 3 | testnet USDC > $20 | `clearinghouseState.marginSummary.accountValue` + spot `USDC.total`; hints when it's all in spot |
| 4 | URL guard | `assert_testnet(HL_API_URL)` passes **and** refuses the mainnet URL |
| 5 | $15 order signs and validates | with DRY_RUN **forced on**: the real adapter builds a $15 ETH IOC market buy plus a stop 5 % below; `validate_order_action` checks ticks and lots per asset, reduce-only trigger legs, tif and grouping, the $10 minimum, and that **the SDK re-encodes the wire identically**; every signature must **recover to the signer for testnet** |

How it reports:
- A check whose input is missing is **SKIP**, not FAIL, so the FAIL list names only real
  problems.
- It prints the list, then `Result:`, `Failed:` and `Next:` (the exact next command), and exits
  1 unless everything passes.
- Only public `/info` endpoints are called. The adapter now keeps its last 50 dry-run payloads in
  memory (`dry_run_records`) so the check can inspect exactly what was signed.

**Today's real run** (no account yet):
```
 1. FAIL  Account + agent key in .env   HL_ACCOUNT_ADDRESS is empty; HL_AGENT_KEY is empty
 2. SKIP  Agent authorized for the account   needs both values from check 1
 3. SKIP  Testnet USDC > $20   needs HL_ACCOUNT_ADDRESS (funds not checked)
 4. PASS  Testnet URL guard
 5. PASS  $15 order signs and validates (DRY RUN)   buy 0.0089 ETH IOC limit 1762.5 (~$14.94)
          + stop 1594.6 (limit 1435.1); valid ticks/lots, SDK-identical wire, signature
          recovers on testnet (throwaway key)
Result: FAIL - 1 failed, 2 skipped, 2 passed.  DRY_RUN in .env: true
Next: Put your MAIN testnet address in HL_ACCOUNT_ADDRESS and the API wallet's private key ...
```

**`docs/GO_LIVE.md`:** the live sequence, with what to check on the glasses and in the testnet UI
at every step: keys → `golive:check` → a dry-run close on the glasses → `DRY_RUN=false` → **Close
25 %** → **SL to breakeven** (the `positionTpsl` step, with the fallback) → a **forwarded ~$15
open with SL** (plus one deliberate over-limit block) → wrap up.

**Checks:** **307 backend tests pass**:
- `test_payload_audit.py`, 22: SDK rounding examples, a property test over 2,000 prices,
  per-asset lots, trigger slippage on both sides, **our stop wire == the SDK's
  `order_request_to_order_wire` for `basic_tpsl.py`'s request**, SL and open-leg payloads.
- `test_golive.py`, 18: the validator against 8 kinds of tampering, every agent and funds
  outcome, the guard, the payload with agent and with throwaway key, and the full reports for
  "nothing configured", "funds missing" and "all ready".

### 24. Voice commands: push-to-talk, local faster-whisper, constrained grammar (item 2)

**What the SDK and simulator offer** (read from `even_hub_sdk` 0.0.16 `index.d.ts` and the
simulator README):
- `OsEventTypeList.LONG_PRESS_EVENT` (9) and `LONG_PRESS_RELEASE_EVENT` (10).
- `bridge.audioControl(isOpen, AudioInputSource.Glasses | Phone)`.
- `event.audioEvent.audioPcm` (Uint8Array).
- The simulator emits audio from a PC input device as **16 kHz s16le mono, 100 ms = 3200 bytes
  per event**. Its automation API has `long_press` / `long_press_release`.
- This machine has only real microphones (Headset, Microphone, Microphone Array), with no
  virtual loopback. That shapes the exit check below.

**Plugin (`voice.ts`, `PushToTalk`, injected I/O so it's testable):**
- Long press → `{"type":"voice","action":"start"}` + `audioControl(true, Glasses)`.
- Each audio event while held → **one binary WebSocket frame**, sent as-is.
- Release → `audioControl(false)` + `{"type":"voice","action":"stop"}`.
- Auto-stop after `MAX_TALK_MS` = 8 s.
- **The tap-then-long-press OS menu:** a long press within `TAP_GUARD_MS` = 600 ms of a tap is
  ignored ("just hold to talk"). The gesture is "hold", not "tap and hold".
- Offline or a mic failure leaves nothing half-open (cancel + mic off).
- `link.ts` gained `sendJson` / `sendBinary`.
- **Bug avoided:** `currentText()` runs inside `display.init()`, before the push-to-talk object
  exists. As a `const` it would have thrown a TDZ ReferenceError at startup (the Day-1 bug
  class), so it's a `let ... | null`.
- HUD: while held, the header reads `MIC  listening... release to send`, then `MIC  ...`, then
  `MIC "close half"` for 2 s. A note line (`not a command`, or the status answer) appears under
  it. With the link down the normal header is shown.

**Backend STT (`voice.py`, `VoiceEngine`):**
- faster-whisper **1.2.1**, model `base`, **CPU int8**, in `backend/data/models` (gitignored).
  It's downloaded once (~145 MB), then loaded with `local_files_only=True`, so there are no
  network calls afterwards.
- `HF_HUB_DISABLE_TELEMETRY=1`.
- Warm-up runs in the background at startup. `/healthz → voice`.
- Decoding: `language="en"`, `beam_size=1`, `without_timestamps`, `initial_prompt` with the
  command words.
- Guards: < 0.3 s of audio or RMS < 0.004 → "" without running the model; the usual Whisper
  hallucinations ("Thank you.", "you") → ""; max 8 s; one transcription at a time.
- Measured on this laptop:
  - Cold download + load: 28 s. Cached load: 5 s.
  - **~1.6–1.8 s per 1.5–2.5 s phrase.**
  - Results: `close half` → close 50 %, `stop to breakeven` → stop to breakeven, `snooze.` →
    snooze, silence → "".

**Grammar (`voice_grammar.py`, pure).**
- Commands: close 25 / 50 / all (incl. "half", "a quarter", "everything", "flatten", "trim
  half"), stop to breakeven, stop tighter, snooze, cancel ("never mind", "abort"), status.
- How words are matched:
  - Normalisation: number words, `%`, and joining "break even" / "stop loss".
  - An alias table for mishearings ("clothes" / "close have" → close half, "titer" → tighter).
  - `difflib` fuzzy matching with cutoff 0.75, for words of 4+ letters (so "stock" doesn't become
    "stop").
- **"cancel" always wins.** More than one intent → "one command at a time". `close` without an
  amount → "close how much?". `close 30` → refused, not rounded. Opening a position is never a
  voice command.

**Voice only arms** (`ActionFlow.voice`):
- A command goes through the same `_select()` as the matching menu tap, so the policy
  pre-check, the WATCH refusal and the trading check are identical. The two-tap confirm still
  sends.
- "stop to breakeven / tighter" opens the SL submenu, waits for the current stop
  (`fetch_stop` → `pending_voice` → `select_key`), then arms.
- During a confirm only "cancel" is accepted. On the **reply screen any speech replaces the
  draft text** (≤ 60 characters, source `voice`), and "cancel" skips. "status" shows a one-line
  status for 4 s.
- **WATCH-only refusals are now status `blocked`** (title `BLOCKED`), like policy blocks; they
  used to be `rejected`.

**Protocol:**
- Client → server: `{"type":"voice","action":"start"|"stop"|"cancel"}`, plus binary PCM frames.
  Audio outside push-to-talk is ignored and never stored.
- Server → client: `{"type":"voice","state":"listening"|"heard"|"error","text","command","ok",
  "note"}`.
- `hud` frames carry `voice` (the overlay).
- Logs record the command label and the audio length, **never dictated reply text**.

**Demo backend:** `POST /demo/voice {"wav_path": ...}` (localhost-only) queues a 16 kHz mono
WAV as the speech of the next push-to-talk. It **replaces** what the simulator's microphone
recorded **before** transcription, so room audio is never transcribed. It's for machines without
a virtual mic; the long-press, mic and streaming path still runs.

**Checks:**
- **382 backend tests pass** (+75):
  - `test_voice_grammar.py`, 57: 46 understood phrases (17 noisy or misheard) and 11 refusals.
  - `test_voice.py`, 18: engine guards; flow arms but doesn't send, WATCH blocked, precheck,
    cancel and busy, status, snooze, no position, SL waits for the lookup, reply dictation;
    `/ws` push-to-talk with 100 ms binary frames → arms → tap-tap fills; WATCH blocked; audio
    outside push-to-talk ignored; voice off; unknown speech; status; the 8 s cut.
- Plugin: **44 tests** (+7): the `PushToTalk` lifecycle, the tap guard, auto-stop,
  offline / no mic, reset, the long-press mapping, MIC header lines.

### 25. Session summary (screen 6), journal post, /summary (item 3)

**What a session is:** it starts when the backend starts, and again right after one is ended.
**`session.py`:**

| Number | Source |
|---|---|
| trades | every order result that changed something (`filled` / `resting` / `placed`); dry-run signatures counted separately ("+N dry run") |
| realized P&L | `adapter.realized_pnl_since(session_start)` = Σ(closedPnl − fee) of fills since the start, refreshed every 30 s and when the session ends. **New on the adapter interface.** Hyperliquid uses `userFillsByTime` for the own account, **or in WATCH mode for the watched one** (fills are public). The fake keeps a timestamped log. The Bybit stub returns 0 |
| unrealized | Σ uPnL of the positions open now |
| session P&L | realized + (unrealized now − unrealized at the start). Only what changed **during** the session, so a WATCH position opened last week doesn't count |
| max drawdown | the largest peak-to-trough fall of the session-P&L curve, sampled every 5 s and after every result |
| alerts | fired (`AlertService.on_fire`), acted (an order sent from an alert screen, via the alert's SL submenu included: `Outcome.extra["via_alert"]`), snoozed |
| policy blocks | arm-time pre-check blocks, submit blocks, blocked forwarded drafts, WATCH refusals (`Outcome.blocked`) |

`realized_pnl_today()` (the policy's daily loss lock) still looks **only** at the own account,
never at a watched one (tested).

**Screens (`ActionFlow` modes `end` and `summary`):**

| Screen | Tap | Swipe | 2x |
|---|---|---|---|
| HUD | menu | next / prev | **End session?** (was: exit) |
| `END SESSION?  14:02-now (1h28m)` / `Trades 2  Session P&L +$9.30` / `Max drawdown -$18.20` (live) | end it → summary | back to the HUD | exit the app |
| `SESSION 14:02-15:30 (1h28m)` / trades + realized / uPnL now + session / drawdown + blocks / alerts / mode, or `Posted to journal` / `Not posted: <why>` | **post to the journal** | HUD | exit the app |

- **You can still always get out with two double taps** (HUD → End session? → exit).
- With the link down, one double tap exits, as before.
- `commandFromEvent(event, mode, online)` now knows whether the link is open.
- New HUD footer: `tap: menu  swipe: next  hold: talk  2x: end`.

**Journal channel:**
- `JOURNAL_CHAT_ID` (a channel or group; the bot must be an **admin**).
- `TelegramBot.send_to_chat` posts the text summary: span, mode, trades with times (marked
  "from alert"), realized / unrealized / session P&L, drawdown, alerts, blocks, and "Numbers only
  - not advice."
- **Setup helper:** when the bot is added to a channel or group (`my_chat_member`, now in
  `ALLOWED_UPDATES`), it DMs the owner the chat id and the exact `JOURNAL_CHAT_ID=...` line, and
  says if it still needs admin rights.
- **`/summary`** (owner only) answers with the running session so far.
- Every ended session is also stored in a new SQLite `sessions` table (summary JSON, numbers
  only).
- `/healthz → session` shows the live numbers.

**Demo backend:** `send_to_chat` is replaced by a send-only call with the real token (no
polling, so no conflict with the real backend). Posts are visible at `GET /demo/journal`.

**Checks:**
- **392 backend tests pass** (+10, new `test_session.py`):
  - Counting, the session-P&L and drawdown maths (peak +20 → trough −50 = −70, unaffected by a
    +$500 position at the start), ending starts a fresh session.
  - The exact journal text; the screens; `/summary` owner-only.
  - The channel-id DM (admin vs member); WATCH realized P&L uses the watched address.
  - **End to end:** a Close 50 % fill → HUD 2x → `End session?` (live: 1 trade) → tap → the
    summary (realized +$20.60) → tap → posted to chat -1001; the session is stored in SQLite and
    a fresh session runs.
  - Without `JOURNAL_CHAT_ID`, the screen says why.
- Plugin: **46 tests**: End / summary rendering, double tap → back online / exit offline / exit
  on end and summary, the new footers.

### 26. Robustness: re-arm on foreground, STALE indicator, render latency, glyph fallback (item 4)

**Re-arm on `FOREGROUND_ENTER`** (`main.ts`):
- `link.reconnectNow()` (a fresh socket, so a phone that slept with a half-dead socket recovers
  at once) and `ptt.reset()` (mic off, push-to-talk idle and ready). This part landed with §24.
- **New:** whenever the link leaves `open`, `ptt.reset()` runs too, so the mic never keeps
  streaming into a dead socket.

**STALE indicator (> 3 s):**
- The backend now sends a `hud` frame **at least every 1 s** (`HEARTBEAT_S` 5 → 1). Silence
  for 3 s therefore really means something is wrong.
- The plugin re-renders every **1 s** (`CLOCK_TICK_MS` 10 s → 1 s). This is free when nothing
  changed, because `glasses.ts` never re-sends identical text.
- `hud.ts staleness()`:
  - **No frame for > 3 s:** `STALE: no update for 5s`. From 8 s on it adds ` - reconnecting`,
    because the link watchdog (`WATCHDOG_MS`, 15 s → 8 s) then drops the socket.
  - **Fresh frames but old prices** (`feed.age_ms` + frame age > 3 s):
    `STALE: prices 4s old`.
  - **Feed down:** the existing `Hyperliquid feed reconnecting...` line, not a second warning.
- **Header dot** after TESTNET: `●` (or `*`) while fresh, `!` when stale.
- Replaces the Day-1 `Link stale 20s - reconnecting` line, which only appeared after 15 s.

**End-to-end latency (exchange event → render ack):**
- **t0** `MarketState.last_change_ms`: the backend's wall clock when a Hyperliquid message
  changed the state. Hyperliquid's `activeAssetCtx` has no server timestamp, so receive time is
  the event time. Every `hud` frame carries it as **`src_ms`**.
- **t1:** after `textContainerUpgrade` for that frame's text returns `ok`, the plugin sends
  `{"type":"rendered","seq":N,"src_ms":t0}`. The backend notes the arrival time.
- **Both ends are measured on the backend clock**, so phone/laptop clock skew can't distort it.
  The figure includes:
  - the 500 ms throttle
  - the hop to the phone
  - the BLE render
  - the ack's trip back
- Each `src_ms` is acked at most once. A frame whose text never reaches the display (identical,
  or overtaken by a newer frame) gets no ack. Values outside 0–60 s are dropped as implausible.
- `latency.py LatencyStats` keeps a window of 500. `/status` ends with
  `Latency (HL event -> glasses render): p50 X ms, p95 Y ms (n=N)`, and `/healthz → latency` has
  `{n, p50_ms, p95_ms, max_ms}`.
- Plugin wiring: `GlassesDisplay(bridge, log, onRendered)`. `render(beforeShow)` records the text
  of a frame with a new `src_ms` *before* it is queued, so the ack can't race the BLE write.

**Glyph fallback (`GLYPHS` in `.env`):**

| Set | When | Risk | Rule (above the footer) | Live dot |
|---|---|---|---|---|
| `unicode` | `GLYPHS=unicode` (default) | `▲` | `━` × 26 (20 at first; widened after the §28 screenshots) | `●` |
| `ascii` | `GLYPHS=ascii`, if a display shows boxes | `/!\` | `-` × 40 | `*` |
| legacy | frame without `glyphs` (older backend) | `▲` | `-` × 40 | none |

- The `hello` and `hud` frames carry `glyphs`.
- `fit(line, allowed)` turns any non-ASCII character outside the active set into `?`, so a stray
  `●` can never reach the glasses in ascii mode.
- The legacy set keeps every Day-2 screen test byte-identical.
- Whether `━` and `●` render, and how wide 20 `━` are on the G2 font, is checked in the exit check
  (§28).

**Checks:**
- **400 backend tests pass** (+8, new `test_latency.py`):
  - percentiles, the implausible-ack filter, the window
  - over `/ws`: `hello.heartbeat_ms == 1000`, `glyphs`, `src_ms` in range, the ack → `/healthz`
    `latency.n == 1`, and a 120 s-old ack ignored
  - a malformed `rendered` → `bad_message`, with the connection kept
  - the `/status` last line; `GLYPHS=emoji` refused
- **Plugin: 52 tests** (+6):
  - the 3 s / 8 s thresholds (exactly 3 s is still fresh)
  - prices-old vs link-silent, with no double warning when the feed is down
  - the unicode / ascii / legacy headers, rules and alert glyph, including the ascii scrub
  - `staleness()` edge cases
- ruff, mypy and tsc are clean; `npm run build` OK. (The plugin has never had an ESLint config;
  `npx eslint` only reports that.)

### 27. Docs: Bybit fallback plan and the demo storyboard (items 5 and 6)

**`docs/FALLBACK_BYBIT.md`** is a plan only; nothing is implemented, and the stub
`exchanges/bybit_demo.py` now points to it. It is built so a switch takes under 2 hours:

**Environment:**
- `BYBIT_ENV=testnet` is the default, because it keeps "testnet only" literal.
- `demo` (a paper account on mainnet infrastructure, which needs mainnet public price streams)
  is an explicit opt-in.

**The guard:**
- Endpoints are an exact-match allowlist per env. There is no free-form URL variable.
- Auth is only ever sent to the allowlisted REST and private-WS hosts.
- A startup `GET /v5/account/info` must pass, which proves the key belongs to that env.
- WATCH mode is refused on Bybit, because Bybit positions are private.

**The design choice:**
- The Bybit feed translates tickers and positions into the **Hyperliquid shapes `MarketState`
  already eats** (`update_asset_ctx`, `update_clearinghouse`).
- So the HUD, alerts, `/status`, the session summary and the policy guard stay untouched.

**Per method:**
- Close = reduce-only IOC limit at mid ± 5 %, confirmed by polling `order/realtime`.
- SL = `position/trading-stop` (Full, MarkPrice): one call replaces the stop.
- Open = `set-leverage` + `order/create` with the SL and TP attached.
- P&L = `position/closed-pnl` (max 7-day window).
- Rounding is step-based (`qtyStep` / `tickSize` with `Decimal`). Hyperliquid's
  5-significant-figure rule does not apply.

**Verification:**
- Facts were read from Bybit's v5 docs today: URLs, the signing string, WS auth and ping, every
  parameter name.
- Three points are marked **check** because the docs don't say them:
  - whether `closedPnl` is net of fees
  - the `orderStatus` enum names
  - the retCode for "leverage not modified"
- The plan includes a 120-minute budget in six steps, each with a "done when".

**`docs/DEMO.md`: a 3:00 storyboard on a REAL testnet position.**
- Shots, in order:
  - HUD
  - an alert raised by `/threshold` → SL to breakeven
  - voice "close a quarter" → tap → fill
  - a forwarded $15 open with SL → fill, then a forwarded $1,000 open → BLOCKED
  - glasses off → the alert DM and `/status` latency
  - End session → the journal post
  - a safety title card
- Each beat has a proof shot in the Hyperliquid testnet UI.
- A header table says what may be on camera: `DEMO` never, `DRY RUN` only in rehearsal.
- Alternatives are planned, not improvised: a position under water (the guard blocks SL to
  breakeven), a misheard voice command, and the optional Business reply.
- **Checklist:**
  - The day before: funds, `golive:check` all PASS, a full `DRY_RUN=true` rehearsal, glyphs.
  - 30 minutes before: `/healthz` fields, the isolated 5x ~$100 position, and the threshold
    value read off the HUD.
  - Privacy in every frame.
  - Afterwards: `/threshold 5`, because it persists.
- No-funds fallback: the WATCH version, honestly labelled, never the fake exchange.

**Bug found while writing the storyboard:** a forwarded `ETH long market $1000 5x` (no SL) is
blocked for **"Opening orders need a stop-loss (sl)"**, not for the notional, because the SL check
runs first in `policy.py`. The storyboard's over-limit message therefore includes `sl 1630`.
`GO_LIVE.md` already did.

### 28. ✅ Day-3 exit check: simulator + real Telegram (2026-11-25, ~01:55–02:10 IST)

**Setup:**
- The demo backend on :8010 (`scripts/demo.py`), run first in WATCH mode, then with the fake
  exchange.
- Vite on :5174 (`VITE_BACKEND_URL=http://127.0.0.1:8010`).
- Simulator 0.9.5 with `--automation-port 9898`, driven by a scratch script (not in the repo):
  inputs, screenshots, `/demo/*`.
- The user's own backend on :8000 kept running the whole time. The demo backend never polls
  Telegram, so there was no 409.
- **Voice:** the PC has no virtual mic. Each push-to-talk ran the real path (long press → mic
  open → binary PCM frames → release), but `POST /demo/voice` **replaced** the recorded audio
  with a Windows-TTS WAV ("close half", 1.67 s) **before** transcription. Room audio was never
  transcribed or stored. The faster-whisper model ran for real.
- Screenshots: [docs/screenshots/day3/](screenshots/day3/).

| # | Exit-check item | Result |
|---|---|---|
| 1 | WATCH: hold-to-talk "close half" shows the transcript and arms, blocked as watch-only | ✅ WATCH `0x7149...7bd0` (a real BTC long 0.30 @ 59,4xx, 10x). `MIC  listening... release to send` → `MIC "close half"` → `BLOCKED  BTC-PERP` / `Watch only - not your account, nothing sent`. Plugin log: `voice heard: "close half" -> close 50% (Watch only ...)`. The arm goes through the same `_select()` as the menu, so the WATCH guard answers instead of a confirm. Nothing can be sent |
| 2 | Demo backend: the same voice command runs through tap-tap | ✅ `MIC "close half"` / `CONFIRM  Close 50%  ETH-PERP LONG` / `SELL 0.25 ETH  reduce-only IOC` → tap → `SENDING` → `FILLED  Close 50%` / `Filled 0.25 @ 1,667.0` |
| 3 | Raising /threshold sends the alert DM with the glasses closed | ✅ Simulator closed (`ws_clients 0`) → `POST /demo/threshold {"pct":20}` (liq 14.8 % away) → **a real Telegram DM within ~4 s** (`delivered_to: telegram`): `▲ RISK  ETH-PERP LONG 0.25` / `Liquidation 1,420 is 14.8% from mark 1,666.3 (alert at 20%).` On reconnect the delivered alert did not pop up again |
| 4 | Ending a session posts the summary to the journal channel | ✅ HUD 2x → `END SESSION?  02:05-now` → tap → `SESSION 02:05-02:07` (Trades 1, Realized +$4.24, uPnL now +$4.21, Session −$0.28, Max drawdown −$4.79, Alerts 1 fired) → tap → `Posted to journal`. **Delivered to the real channel**: the journal text with the trade line `02:06 close 50% ETH LONG: Filled 0.25 @ 1,667.0` and "Numbers only - not advice." The WATCH summary screen worked too (Realized −$0.16 from the watched account's public fills, `Blocks 1` = the watch-only refusal) and was not posted, to keep the channel to one test post |
| 5 | `npm run golive:check` prints a clean FAIL list | ✅ `1. FAIL Account + agent key in .env (HL_ACCOUNT_ADDRESS is empty; HL_AGENT_KEY is empty)`, `2./3. SKIP` (agent, funds: need the address), `4. PASS` guard, `5. PASS` $15 payload (buy 0.0089 ETH IOC limit 1757.1 + stop 1589.8, limit 1430.8). `Failed:` names only the keys. Exit code 1 |

**Robustness (item 4), seen live:**
- **Glyphs render** on the G2 framebuffer: `●` after TESTNET, `━` as a solid rule, `▲` (§20).
  20 `━` spanned only ~400 of 576 px, so the rule is now **26** (~520 px).
- **STALE on a silent link:** the backend *process* was suspended for 11 s (`NtSuspendProcess`),
  which kept the socket open but silent. That's the case STALE is for; killing the process would
  just show "Backend unreachable".
  - At 4.5 s: `TESTNET !` and `STALE: no update for 5s`.
  - At 9.5 s: `STALE: no update for 10s - reconnecting`.
  - After the resume, the plugin reconnected by itself (a fresh `hello`).
- **Latency** (HL event → glasses render, simulator on the same laptop):
  - WATCH: p50 195 ms / p95 513 ms (n=19), later **p50 70 ms / p95 521 ms (n=65)**.
  - Demo: p50 125–264 ms / p95 793–845 ms.
  - The p95 is dominated by the 500 ms frame throttle.

**Bugs found and fixed during the exit check:**
1. **Session P&L counted an existing position's whole uPnL.** In WATCH mode the summary said
   **−$75.62 seconds after startup**, with 0 trades.
   - Cause: `SessionService` took its uPnL baseline at construction, before the feed had
     delivered the watched account. There, `positions == []` means "unknown", not "flat".
   - It would hit an own account the same way.
   - Fix: `MarketState.positions_loaded` (set by the first `clearinghouseState`; true when there
     is no account). The baseline waits for it **and** for a live mid on every open position.
     Without the mid, the first uPnL is priced off `positionValue` and jumps when the mid lands.
     After the fix: −$2.97 → a real move, then the expected behaviour.
   - Tests: `test_baseline_waits_for_the_first_positions_snapshot`,
     `test_baseline_also_waits_for_a_live_mid`, `test_price_only_and_fake_adapter_have_a_baseline_at_once`.
2. **Flaky test** `test_trade_then_end_session_then_post` (failed 2 of 4 full runs).
   - The fake fills instantly, so the confirming tap's `ack` can arrive **after** the `order`
     frame. The next `recv(ws, "ack")` then read that stale ack, and every later assertion was
     one step behind.
   - Test-only: on a real exchange the order takes far longer than the ack.
   - Fix: wait for the `back` acks by `action`. Now 6/6 repeated runs and the full suite pass.
3. **Cosmetic:** after a voice command that ends on a result (the WATCH refusal), the voice note
   repeated the result's own message, so the same line showed twice. `renderHud` now skips the
   note when the result already shows it (test added).

**Checks after the fixes:** **403 backend tests pass**, ruff, mypy and tsc are clean, **plugin
52 tests pass**.

**Docs refreshed:**
- README: Day-3 features, voice table, journal setup, STALE / voice / glyph troubleshooting,
  privacy (voice stays local), faster-whisper in the OSS table, the PowerShell `npm --` gotcha.
- `architecture.md`: the diagram, latency / voice data flow, failure table, config table, "Next".
- `ws-protocol.md` (§26).

**Cleanup:**
- The demo threshold was reset to 5 (the demo DB, `backend/data/demo.sqlite3`).
- The simulator, demo backend and Vite were stopped; ports 5174 / 8010 / 9898 are free.
- The user's :8000 backend runs with `--reload`, so it picked up the session fix by itself.
- Learned: in PowerShell, `npm run backend:demo -- --watch X` loses the `--` (`npm.ps1`), so
  call `uv --directory backend run python scripts/demo.py --watch X` directly (README updated).

The Day-3 commits (`4d319be`…`9756d1e`) were pushed to `origin/main` with the user's OK.

---

## 2026-11-26 · Day 4

### 29. Positioning update from the organizers' meeting

What the user brought back from the meeting:
- **The Even team confirmed that plugins have NO background or push path to the glasses.** A
  plugin runs only while it is open, so nothing outside can wake the G2 or put text on it.
- **Consequence:** the Telegram-DM alert is not a fallback; it is the only way to reach a trader
  who isn't wearing the glasses with Tripwire open. It becomes a core feature, **"Away mode"**:
  1. With no glasses client connected, risk alerts go to a Telegram DM with a one-line position
     summary and the instruction "Open Tripwire on your glasses to act".
  2. When the plugin reconnects with an unresolved alert, it opens straight on the ▲ RISK
     screen. The alert persists until it is acted on, snoozed or resolved.
  3. A dedup / cooldown, so the DM can't spam: one per alert episode.
  4. Tests for "alert while away → DM once" and "reconnect → RISK screen first".
- **README:** "Why Telegram + glasses", "Who it's for" and "API feature requests" sections, with
  #1 being a background alert / push slot for plugins.
- **`docs/DEMO.md`:** the storyboard must show glasses closed → the DM arrives → open Tripwire →
  the RISK screen → a voice / tap action → the two-tap confirm.

### 30. Away mode: alert episodes, one DM per episode, RISK screen first on reconnect

**Alert episodes (`risk.py`):** an episode opens on a position's (coin + side) first fire and
stays open until one of these happens:

| Ends it | How | Where |
|---|---|---|
| **acted on** | a close or stop on that position that **really changed something** (`filled` / `placed` / `resting`). A **dry run, a block or a rejection doesn't count**: the risk is still there | `main.on_order_result`, called from `ws.run_order` for every result |
| **snoozed** | `Snooze 5m` (menu or voice). If the position is still inside after the snooze, the next fire is a new episode | `main.on_snooze` |
| **resolved** | the position closed, or its distance is back ≥ threshold + hysteresis (the engine re-arms at the same line) | `AlertService._resolve`, on every check |

- **Backing out with a double tap does not end it**, so the next connect shows the RISK screen
  again.
- Episodes are in memory only. After a backend restart, an alert still inside fires again as a new
  episode.

**Away mode delivery (`AlertService.deliver` / `_away_dm`):**
- **Glasses connected:** the RISK screen, as before (no DM).
- **No glasses:** a DM, at most **one per episode**. Re-fires inside the same episode (for
  example `/threshold` re-arming everything) are reported as
  `telegram (already sent for this alert)`.
- **Per position, never two DMs within `DM_MIN_GAP_S` = 600 s.** A price wobbling around
  threshold + hysteresis closes and reopens episodes; the gap keeps that from becoming a DM storm
  (`telegram suppressed (last DM for ETH LONG 60s ago)`).
- The session's "alerts fired" now counts **episodes**, not re-fires.
- **Not done on purpose:** an episode already shown on the glasses doesn't DM when the glasses
  disconnect. The user saw it, and it will reopen on the RISK screen anyway.

**The DM** (`Alert.text()`; `Alert` gained `entry`, `leverage`, `upnl`, filled by
`Alert.from_position`):
```
▲ RISK - Away mode
ETH-PERP LONG 0.5 @ 1,650 5x | uPnL +$14.90 | liq 1,420: 15.5% from mark 1,679.8 (alert at 20%)
Open Tripwire on your glasses to act.
```

**RISK screen first (`ws.py`, on connect):** `active_alert()` returns the most urgent open
episode, **rebuilt from the live position**, so the distance is current and not the one from
when it fired. It goes on screen before the first frame. A forwarded draft that was waiting for a
client becomes the flow's `pending_draft` and shows once the alert is left.

**Visible state:**
- `/status` has a new line: `Away mode: ON - no glasses connected, risk alerts come here` (or
  `off - glasses connected (1)`), plus `Open alerts: ETH LONG 2.3% from liq` while any are open.
- `/healthz → alerts` gained `open`, `dms_sent`, `dms_suppressed` and `away_mode`.
- Protocol: `alert` has 3 new optional fields, and there are no new messages
  (`ws-protocol.md` "Day 4").

**Also fixed (seen in the simulator):** after a spoken "stop to breakeven", the heard line's note
`loading the current stop-loss` stayed on the confirm screen after the stop had loaded. `load_stop`
now replaces the note with the outcome of the waiting command: nothing once the confirm is armed,
or the reason it wasn't. A test was added.

**Tests: 413 backend pass** (+10). New `test_away_mode.py` (9):
- the DM text
- **alert while away → DM once** (deeper inside and a `/threshold` re-fire send nothing more)
- the wobble cooldown, and a real new episode after the gap
- episode ends (snooze, position gone, a snooze-expiry re-fire = new episode)
- shown on glasses = no DM; `active_alert` picks the most urgent with live numbers
- **end to end:** away → 1 DM → `/status` "Away mode: ON" → connect → **the first frame is the
  RISK menu** with the live distance → Close 50 % via the two-tap confirm → closed → reconnect →
  HUD
- snooze ends it, and a waiting draft comes after the alert
- only a real change ends it (dry run, blocked, rejected and another coin don't)

Also changed: `test_alerts.py` (the DM text), `test_telegram_depth.py` (the `/status` line) and
`test_voice.py` (+1, the note). Three full runs in a row: all green. Plugin: 52 pass (a type-only
change in `protocol.ts`).

**Simulator check (2026-11-26, ~01:35 IST; fake-exchange demo backend; screenshots in
[docs/screenshots/day4/](screenshots/day4/)):**
1. Glasses closed (`away_mode: true`) → `/demo/threshold 20` → **one DM** (text above) →
   `/demo/threshold 21` re-fired the open episode → still one DM (`dms_sent 1, dms_suppressed 1`).
2. Simulator opened → **the first screen is `▲ RISK  ETH-PERP LONG 0.50` / `Liq 1,420  15.6% from
   mark (<21%)`** with Move SL / Close 50% / Close all / Snooze 5m. These are live numbers: 15.6 %
   now vs 15.5 % at the fire, and the current 21 % threshold.
3. Hold, say "stop to breakeven" (a queued TTS WAV replaced the mic audio, as on Day 3) →
   `MIC "stop to breakeven"` → `CONFIRM  SL to breakeven` / `SL 1,500 -> 1,650  reduce-only` →
   tap → `PLACED` / `SL set at 1650, old SL cancelled`. The episode closed (`open: []`).
4. Closed and reopened → the HUD, not the alert.

**Incident:** that one DM **reached the user's real Telegram**.
- The plan was to record it only, by blanking the token with `$env:TELEGRAM_BOT_TOKEN=''`. In
  Windows PowerShell, assigning `''` **deletes** an environment variable, so pydantic-settings
  read the token from `.env`.
- The dedup held for the rest of the check, so nothing else went out. The user was told.
- Fix: `scripts/demo.py --no-telegram` records DMs and journal posts and never sends them
  (README updated). Use it for verification runs.

### 31. Docs: Who it's for, Why Telegram + glasses, API feature requests, DEMO storyboard

- **README:**
  - The intro now describes Away mode.
  - New **Who it's for**: active perps traders holding leveraged positions away from their
    screen.
  - New **Why Telegram + glasses**: no background path on the platform (confirmed by the Even
    team), and Telegram is where traders already are.
  - Telegram section: the Away-mode DM example and the episode rules.
  - New **API feature requests**: #1 is a background alert / push slot for plugins.
  - Two troubleshooting rows ("only one DM", "keeps opening on the RISK screen") and the
    `--no-telegram` note.
- **`docs/DEMO.md`:**
  - **0:22–1:05 is now the Away-mode loop**: close Tripwire → `/status` says Away mode ON →
    `/threshold` → the DM → open Tripwire → **lands on ▲ RISK** → hold "stop to breakeven" →
    confirm → tap → `PLACED`, with proof in Open Orders.
  - The old "glasses off" beat at 2:05 became a `/status` shot (Away mode off + latency).
  - New planned alternatives: the DM doesn't come (check Away mode; the 10-minute cooldown
    before a retake), and under water → "close half" on the RISK screen.
  - Checklist: the bot chat open on the phone; no recent alert for the position.
  - The WATCH fallback now includes the Away-mode loop.
- **`architecture.md`:** the risk.py row, and two failure-table rows (Away mode, reconnect with
  an open alert).
- **`ws-protocol.md`:** a "Day 4: Away mode" section.

The Away-mode commit `f89fadc` was pushed with the user's OK.

---

## 2026-11-26 · Day 5: lock the build

Rules carried over (BUILD_LOG on every change, a commit per milestone, ask before pushing, no
secrets, testnet only, reduce-only, two-tap, no advice). New rule: **`--no-telegram` for any test
that shouldn't message the user.**

### 32. Gap check: session summary, journal, golive:check, docs

| Feature | Status | Evidence |
|---|---|---|
| Session summary screen | ✅ | simulator 2026-11-26: [02-session-summary.png](screenshots/day5/02-session-summary.png) (Trades 1, Realized +$8.24, Session −$0.57, drawdown, alerts, blocks); `test_session.py` (13 tests) |
| "End session?" confirm (HUD double tap) | ✅ | [01-end-session-confirm.png](screenshots/day5/01-end-session-confirm.png); `test_double_tap_on_hud_asks_then_tap_ends_then_tap_posts` |
| `/summary` (owner only) | ✅ | `test_summary_command_is_owner_only`; real Telegram (Day 3, §25) |
| Posting to `JOURNAL_CHAT_ID` | ✅ | `test_trade_then_end_session_then_post`; **a real channel post** in the Day-3 exit check (§28, `delivered=True`) |
| **Dry-run print when `JOURNAL_CHAT_ID` is unset** | ✅ **new today** (it used to only say "Not posted") | [03-journal-dry-run-print.png](screenshots/day5/03-journal-dry-run-print.png): `DRY RUN - printed to the backend log`; the backend log has `JOURNAL DRY RUN (JOURNAL_CHAT_ID not set), would post:` and the full journal text; `test_post_without_journal_is_a_dry_run_print` |
| `npm run golive:check` | ✅ (FAIL only on the key, as expected) | run 2026-11-26 01:52: `1. FAIL Account + agent key in .env`, `2./3. SKIP` (agent, funds: need the address), `4. PASS` guard, `5. PASS` $15 payload (buy 0.0089 ETH IOC limit 1766.6 + stop 1598.4); `test_golive.py` (18) |
| `docs/GO_LIVE.md` | ✅ | steps 0–8 (§23) |
| `docs/FALLBACK_BYBIT.md` | ✅ | plan only, 120-minute budget (§27) |
| Live testnet orders | ⏳ | waits for testnet funds (not a build gap) |

**What changed:**
- `main.post_journal`: with `JOURNAL_CHAT_ID` unset, it logs the exact text it would post and
  returns `JOURNAL_DRY_RUN_NOTE` (`DRY RUN - printed to the backend log`).
- `hud.ts` shows a `DRY RUN...` note as it is, instead of `Not posted: ...`.
- `scripts/demo.py --no-journal` simulates an unset `JOURNAL_CHAT_ID`.
- The simulator run used `--no-telegram --no-journal`, so nothing reached Telegram.

### 33. Fresh-clone test: README as a new Windows/PowerShell user

`git clone https://github.com/Tejasv-Singh/tripwire.git` (at `f89fadc`) into a temp folder,
then the README followed step by step. Configuration: **only a Telegram bot token** (copied
into the clone's `.env` without being displayed; the clone's env files were deleted
afterwards). WATCH mode, simulator.

| Time (IST) | Step | Result |
|---|---|---|
| 01:52:42 | `git clone` | 3 s |
| 01:53:32 | `npm run setup` | ✅ 47 s (uv and npm caches warm on this machine; expect a few minutes on a truly new one). npm prints "6 vulnerabilities (3 moderate, 1 high, 2 critical)": **all in dev tooling** (vitest, vite / esbuild, tinypool); `npm audit --omit=dev` finds 0 |
| 01:53:43 | `Copy-Item .env.example .env` | ✅ (the README gave no command; added) |
| 01:54:38 | `npm run watch:find` | ❌ **`httpx.ReadTimeout` traceback after 55 s**: one testnet `/info` call took > 10 s and killed the whole search |
| 01:58:47 | `npm run watch:find` (fixed) | ✅ 35 s, 8 addresses with positions |
| 01:59:05 | `.env` (`PAIRING_CODE`, `WATCH_ADDRESS`, `TELEGRAM_BOT_TOKEN`), `plugin/.env.local` | ✅ |
| 01:59:50 | `npm run backend:dev` + `npm run plugin:dev` | ✅ both up in 10 s |
| 02:00:18 | `npm run sim` | ✅ **`TRIPWIRE WATCH  TESTNET ●` / `BTC-PERP SHORT 0.2862 @ 59,595 10x` / `Liq 64,552 8.2% away`**; `/healthz`: Hyperliquid connected, `telegram: running @tripwire_g2_bot`, latency p50 26 ms |
| 02:00:30 | voice model | ✅ first download (142 MB) ready about 40 s after the backend start |

**Total: 7 min 36 s from `git clone` to the live HUD**, including ~3 min lost to the
`watch:find` failure; about 4.5 min without it. Add ~2 min for a new user to create a bot in
BotFather and a minute or two of typing. **Well under the 15-minute target.** The user's own
backend was not running, so there was no second poller on the token (409).

**Fixed from the run:**
1. **`watch:find` timeout.** `/info` now has a 20 s timeout and one retry. A failing address is
   skipped (`(skipped 0x1234...: ReadTimeout)`) instead of crashing, and a dead API prints a plain
   "try again in a minute".
2. **`plugin/.env.example` pointed `VITE_BACKEND_URL` at the author's ngrok domain.** A new user's
   simulator would have shown "Backend unreachable". The default is now
   `http://127.0.0.1:8000`, with the tunnel case explained.
3. **The README told everyone to run `npm run tunnel`.** That's the author's reserved ngrok
   domain, which nobody else can claim. The new **Quick start (simulator + WATCH, Windows /
   PowerShell)** needs no ngrok, and the full setup explains using your own domain (`tunnel`
   script, `app.json` whitelist, `VITE_BACKEND_URL`).
4. **The pairing-code command used `python`**, which may not exist on Windows. It's now
   `uv --directory backend run python -c ...`.
5. The `npm run setup` vulnerability warning is explained in the README (dev-only, 0 in what
   ships).

### 34. Packaging: tripwire.ehpk

- `npm run pack` (new root script; `plugin:pack` still works) runs
  `tsc` + `vite build` + `evenhub pack app.json dist -o tripwire.ehpk --sdk-ver 0.0.16`.
- **Validates:** `{"package_id":"com.tejasvsingh.tripwire","version":"0.1.0","min_app_version":"2.2.10","warnings":[],"ok":true}`
  (`--json`). Output: `plugin/tripwire.ehpk`, **45,683 bytes**, `EHPK` container.
- `app.json`:
  - name `Tripwire`, `package_id` `com.tejasvsingh.tripwire`
  - `network` whitelist `https://` + `wss://caucus-strategic-endorse.ngrok-free.dev`
  - `g2-microphone`: the description was **"Reserved for upcoming voice commands; not used yet"**
    and is now the real push-to-talk use
- Checked:
  - The production bundle contains the ngrok origin (from `plugin/.env.local`) and **not** the
    dev pairing code.
- `--check` (is the package id free?) and `create` / `upload` need `evenhub login` to the
  user's Even account, so they were not run.
- `*.ehpk` stays gitignored: it embeds one person's tunnel URL and changes with every build. A
  GitHub release asset is the place for it, if the user asks.
- The README has a "Build the installable package (.ehpk)" section.

### 35. Submission drafts: docs/SUBMISSION.md

- **Short description:** the problem, who it's for, how it works, and safety. The tracks are
  written as 02 + 01 (Crypto & DeFi, Messaging), **with the numbering marked "confirm before
  submitting"**: the repo never recorded which is which. It also covers why it fits **Best
  Telegram integration**, with a feature-by-API table.
- **API feature requests: Even (5).**
  - background alert / push slot (confirmed missing by the Even team)
  - an OS-owned trusted confirm sheet
  - secure keystore storage
  - dashboard widgets
  - in-place list updates plus `eventSource` on every gesture
  
  Checked against SDK 0.0.16's `index.d.ts`:
  - Lists can only be set by create / rebuild; only text and images have upgrade calls.
  - `eventSource` exists only on `Sys_ItemEvent`.
  
  Also smaller ones: a monospace font, the tap-then-long-press OS menu, and a simulator audio
  input.
- **API feature requests: Hyperliquid (6).**
  - `webData2` docs drift
  - no server timestamp on `activeAssetCtx`
  - multi-coin mark prices
  - `positionTpsl` docs
  - a builder faucet
  - SDK `WebsocketManager` can't route `clearinghouseState`
- **API feature requests: Telegram (3).**
  - Business needs Premium
  - first-day `getMe` throttling (~18 min)
  - "Secretary Mode" naming
- **"What existed before Nov 23":** research notes and a plan only, plus the full OSS table
  (now including faster-whisper and the Whisper model).
- The README's API section now lists all five Even requests and links the details.

**Checks:** 413 backend and 52 plugin tests pass; ruff, mypy and tsc are clean.

### 36. Track numbering confirmed (Day 5 wrap-up)

The user confirmed from the official brief: **Track 01 = Messaging & News**, **Track 02 =
Crypto & DeFi**. Tripwire enters **Track 02 (main) + Track 01**, plus **Best Telegram
integration**.
- `README.md` (intro line) and `docs/SUBMISSION.md` (the tracks line) now use the official
  numbers and names.
- The "confirm the track numbers" item was removed from SUBMISSION's checklist.
- This supersedes the "confirm before submitting" note in §35.

`463e007` and this fix (`cc67a40`) were pushed to `origin/main` with the user's OK.

### 37. DEMO.md: a timed 2:45 shot list

The 3:00 storyboard became a **shot list**: timestamp | on screen (simulator left / Telegram
right) | voiceover | the action the user performs.

| Time | What happens |
|---|---|
| 0:00–0:10 | **The problem in one sentence** |
| 0:10–0:25 | glance at the HUD |
| 0:25–0:40 | glasses off → `/status` says `Away mode: ON` |
| 0:40–0:55 | `/threshold N` → the Away-mode DM |
| 0:55–1:10 | reopen → lands on ▲ RISK |
| 1:10–1:30 | hold, "stop to breakeven" → confirm |
| 1:30–1:40 | tap → `PLACED` |
| 1:40–2:00 | forwarded $15 idea → draft → two taps → `FILLED` |
| 2:00–2:10 | $1,000 idea → `BLOCKED` |
| 2:10–2:25 | End session → journal post |
| 2:25–2:35 | **the safety line** |
| 2:35–2:45 | **the pattern line: "glance at your glasses, hear from Telegram when you're away, and act with a two-tap confirm"** |

The rows add up to exactly 165 s.

- **A WATCH variant**, row by row: voice ends in `BLOCKED watch only`, then "snooze" ends the
  alert; the drafts show the watch-only block.
- Planned alternatives: under water, no DM, misheard, and the confirm expired. The 5-second
  confirm window is called out at 1:30.
- **The checklist covers the requested items:**
  - the real backend, never `--no-telegram`
  - ngrok with your own domain
  - a WATCH address or a live position
  - a threshold that triggers (N = the liq % rounded up + 2)
  - the journal channel with the bot as admin
  - the **OBS scene**: the simulator left, the phone Telegram mirror right, a "Glasses off" card,
    and the PC mic as the push-to-talk input
- **Between takes:** `npm run demo:reset`, restart the simulator, and on a live account cancel
  the placed stop and close the $15 open.

### 38. `npm run demo:reset`: a clean slate between takes

`scripts/demo_reset.py` POSTs `/admin/demo-reset` on the running backend (default `:8000`;
`--url` for the demo backend).

**It clears in-memory demo state only:**
- `AlertService.reset`: open episodes, Away-mode DM cooldowns, the DM counters, the engine's
  armed / snoozed state and its fire count; the threshold goes back to `ALERT_LIQ_PCT` and is
  persisted.
- `SessionService.reset`: a fresh session, with the aborted take **not** stored.
- `ClientHub.reset_clients`: every connected client's flow goes back to the plain HUD (timers
  cancelled, voice overlay cleared). `ActionFlow.reset` keeps its token counting, so old timers
  stay stale.
- A waiting draft, the inbox line and the latency samples.

**It never touches:** `.env` (only read, for the pairing code), exchange positions and orders,
the kill switch, or the stored session history.

**Guard:**
- The pairing code is required as `X-Pairing-Code` (401 otherwise).
- A request carrying `X-Forwarded-For` (which ngrok adds) gets 403. Through the tunnel a request
  would look local, so "localhost only" alone wouldn't be enough.
- POST only; CORS allows only GET.

**Backend not running:** everything above starts fresh on the next start anyway. The script only
resets the saved threshold in SQLite.

**Tests: 416 backend pass** (+3, new `test_demo_reset.py`):
- the guard (no code, wrong code, via the tunnel, GET)
- a full take, then a reset clears episodes, cooldowns, threshold (and the saved one), session,
  latency, and the connected client's screen; the exchange is untouched, and the next take DMs
  again at once
- the offline path

**Live smoke** on the demo backend with `--no-telegram`:
- Before: threshold 20 %, 1 open alert, 1 recorded DM.
- `uv --directory backend run python scripts/demo_reset.py --url http://127.0.0.1:8010` printed
  the summary.
- After: threshold 5 %, `open: []`, `fired 0`, 0 trades.

Also: `scripts/demo.py`'s DM log line said `RECORDED (no owner id)` when the real reason was
`--no-telegram`. It now names the reason.

README: a `demo:reset` paragraph, and the DEMO link text. SUBMISSION's checklist points to the
shot list.

`9421e9a` was pushed to `origin/main` with the user's OK.

---

## 🧊 FEATURE FREEZE: from 2026-11-26, 18:15 IST (at `9421e9a`)

Declared by the user. From here on, **only** these changes are allowed:

| Allowed | Examples |
|---|---|
| **Bug fixes** | something that is broken or wrong in an existing flow, with a test that shows it |
| **Live-order go-live** | `docs/GO_LIVE.md` steps once the testnet funds arrive, including the one-line `positionTpsl` size change in `set_sl` if step 6 is rejected, and whatever the first live orders show to be wrong |
| **Doc / submission edits** | README, `docs/*.md`, `SUBMISSION.md`, `DEMO.md`, BUILD_LOG entries, screenshots, the demo video, a GitHub release with `tripwire.ehpk` |

**Not allowed:**
- new features, screens, commands or settings
- refactors without a bug behind them
- dependency upgrades, including the dev-only `npm audit` items (vite 5 → 7 / vitest 2 → 4)

When in doubt, it's out. Every change under the freeze still follows the standing rules:
- a BUILD_LOG entry that says which allowed category it falls under
- the tests green
- a commit per change
- ask before pushing
- no secrets
- testnet only, reduce-only by default, the two-tap confirm, no advice

Frozen state:
- backend 416 tests, plugin 52
- `npm run pack` → `tripwire.ehpk`, validated
- the fresh clone runs in 7 min 36 s
- docs complete (GO_LIVE, DEMO, FALLBACK_BYBIT, SUBMISSION)

Open items allowed under the freeze:
1. the live testnet orders (waiting for funds)
2. the demo video
3. the submission form
4. trying it on real G2 glasses, with fixes only if something is broken

`f688883` (this freeze note) was pushed with the user's OK.

---

## 2026-11-28 · go-live prep

### 39. `npm run agent:new`: the agent key is created locally and never shown *(freeze category: live-order go-live)*

**Why:** the user pasted instructions for making the agent wallet locally:
```
pip install eth-account hyperliquid-python-sdk
python -c "... Account.create(); print(a.address); print(a.key.hex())"
```
Then put the key in `.env` and clear the terminal. Generating the key locally, so it never
touches the browser, is a good idea. But as written:
- **it prints the private key** into the terminal. In the Claude app's Terminal panel, that
  output is readable by the assistant, which breaks the "no secrets in chat" rule, and "clear
  the terminal" doesn't always clear the scrollback.
- **`pip install` isn't needed** (both packages are already in the backend's uv environment),
  and that environment has no `pip`, so it would install into the global Python.

I didn't run it: creating an access key is the user's call.

**What:** `backend/scripts/new_agent_key.py` (`npm run agent:new`):
- `eth_account.Account.create()`, then the key is written straight into the repo-root `.env` as
  `HL_AGENT_KEY`. Every other line and its line ending are untouched; `DRY_RUN` isn't changed.
- It **prints only the public agent address** and the next steps: paste the address on the
  testnet API page instead of Generate, then Authorize with the main wallet; set
  `HL_ACCOUNT_ADDRESS`; `npm run golive:check`.
- It **refuses to overwrite** a non-empty `HL_AGENT_KEY`. `--force` replaces it, then the old
  agent should be revoked.
- No `.env` → it says to copy `.env.example` first.

The key format matches the config's own validator (64 hex characters; eth_account 0.13.7 gives
them without `0x`).

**Tests: 421 backend pass** (+5, `test_new_agent_key.py`, temp `.env` only):
- it fills the empty line, keeping CRLF
- it refuses / forces
- it appends a missing line
- `main()` writes a key that `Settings` accepts, its output contains the address and **not the
  key**, and a second run refuses
- no `.env` → it says what to do

**Docs:** `GO_LIVE.md` step 1 and the README's trading steps list `npm run agent:new` as the
recommended way, with the site's Generate button as the alternative.

**Not verified:** that the testnet API page accepts a pasted address instead of Generate. It was
never tried in this project. If it only offers Generate, use that path (copy the key once).
`golive:check` check 2 confirms either way.

`a3d02c3` was pushed with the user's OK.

### 40. ✅ `golive:check`: all 5 PASS for the first time *(freeze category: live-order go-live)*

The user set `HL_ACCOUNT_ADDRESS` (the main wallet, `0x3f27...27ab`) and an authorized agent
key.
- `.env` had **no `DRY_RUN` line**. The backend default (`dry_run = True`) applied, so it was
  already in dry run.
- At the user's request, an explicit `DRY_RUN=true` line was appended. No other line was
  touched; `.env` is gitignored.

Run on 2026-11-28 ~16:35 IST:
```
 1. PASS  Account + agent key in .env      account 0x3f27...27ab, agent key set (format OK)
 2. PASS  Agent authorized for the account 0xe260...EF24 is an agent of 0x3f27...27ab
 3. PASS  Testnet USDC > $20               $999.00 (perps $0.00 + spot $999.00) - most is in spot
 4. PASS  Testnet URL guard
 5. PASS  $15 order signs and validates (DRY RUN)
          buy 0.009 ETH IOC limit 1736.4 (~$14.88) + stop 1571.1 (limit 1414); signature
          recovers on testnet (agent key 0xe260...EF24)
Result: PASS - 0 failed, 0 skipped, 5 passed.  DRY_RUN in .env: true
```

**Before the first live order:** all $999 is in **spot**, and **perps has $0**. Orders need
perps margin, so move USDC from spot to perps on the testnet site first (Portfolio / transfer).
A $100 position at 5x needs ~$20 of margin; moving ~$200 leaves room.

Next: `docs/GO_LIVE.md` step 3 (a dry-run close on the glasses, after restarting the backend so
it loads the key), then step 4 (`DRY_RUN=false`).
