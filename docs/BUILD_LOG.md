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
