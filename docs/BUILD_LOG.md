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
