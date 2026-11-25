# Glasses ↔ backend WebSocket protocol (v2)

One WebSocket per glasses client. JSON text frames only. Source of truth:
`backend/src/tripwire/ws.py` + `hud.py` + `actions.py` (server) and `plugin/src/protocol.ts`
(client types). Change both sides together and bump `v` on breaking changes.

**v2 (Day 2) vs v1:** the `actions` intent is gone; there are now `tap` and `back`. Every `hud`
frame carries `ui` (which screen to draw: HUD, menu, confirm, sending or result) and `trading`.
`position` has `roe_pct`. `ack` has `mode` and can be `ok: false`. There's a new `order` frame. The
backend owns the menu and the two-tap confirm; the plugin only draws what `ui` says.

## v2 additions (Day 2 continued, all additive: `v` stays 2)

The sections below describe the Day-2 shape. These fields were added on top. A client that
ignores them still works, and a missing `kind` / `action` means the Day-2 meaning.

**`hud` frame:**

| Field | Meaning |
|---|---|
| `watch` (bool) | positions belong to `WATCH_ADDRESS` (someone else's, read-only). The plugin shows `TRIPWIRE WATCH` and a tap answers `watch only` |
| `address` (str) | the streamed address, shortened (`0x7149...7bd0`), or `""` |
| `inbox` (object \| null) | Telegram Business messages of the last 10 min: `{"count": 1, "from": "@alice", "preview": "<first 30 chars>"}` |
| `trading.dry_run` (bool) | orders are signed and logged, not sent. The plugin shows `TRIPWIRE DRY RUN` |
| `trading.exchange` (str) | `hyperliquid` / `fake` (`TRIPWIRE DEMO`) / `bybit_demo` |

**`ui` with `mode: "menu"` has a `kind`:**

| `kind` | `items` | extra fields |
|---|---|---|
| `actions` | Close 25% / Close 50% / Close 100% / Move SL... / Back | — |
| `sl` | SL to breakeven / SL tighter 1% / Back | `sl_loaded` (bool), `current_sl` (number \| null, fetched from the exchange when the submenu opens) |
| `alert` | Move SL / Close 50% / Close all / Snooze 5m | `alert: {coin, side, size, liq_px, liq_dist_pct, mark, threshold_pct}` |
| `draft` | Place order / Discard (only Discard if blocked) | `draft: {coin, side, notional_usd, leverage, entry_px (null = market), sl_px, tp_px, defaults[]}`, `policy: {allowed, rule, reason}` |

An `alert` or `draft` menu appears **on its own**: risk.py or a forwarded Telegram message
raises it. If a confirm or an order is in progress, it waits until that screen is left.

**`ui` with `mode: "confirm"` / `"sending"` has an `action`:**
- `close`: the fields documented below.
- `stop`: `old_sl` (null if none), `new_sl`, `mark`, `entry`, `size`.
- `open`: `notional`, `leverage`, `entry` (null = market), `sl`, `tp`, `size` (an estimate), `exact`.

**New `ui` mode `reply`** (after a real fill, if a business chat wrote in the last 30 min):
```json
{"mode": "reply", "coin": "ETH", "to": "@alice", "text": "Moved my ETH stop to 1,650.",
 "source": "template", "result_status": "placed", "result_message": "SL set at 1650; old SL cancelled"}
```
Intents on this screen: `tap` sends the reply and returns to the HUD; `back` skips; `next` /
`prev` do nothing. It skips itself after 20 s.

**Result statuses** (`ui.status`, `order.status`): `filled`, `resting`, `placed` (trigger orders,
stop moves), `rejected`, `error`, **`blocked`** (policy guard; nothing sent) and **`dry_run`**
(signed, logged, not sent).

**Frames:**
- `order` gains `kind` (`close` / `stop` / `open`). `fraction` is `null` except for closes.
- New `{"type": "reply", "ok": true, "to": "@alice"}` after a reply was sent (or failed).

**`ack.note`** may also say: `watch only`, `draft discarded`, `alert snoozed 5 min`,
`sending reply to @alice`, `reply skipped`, `loading the current stop-loss - tap again`, or
`Blocked: <policy reason>`.

## Day 3 additions (still additive: `v` stays 2)

**Push-to-talk (voice.ts / ws.py / voice.py):**

| Direction | Frame | Meaning |
|---|---|---|
| client → server | `{"type":"voice","action":"start"}` | long press began; the mic is open |
| client → server | **binary** frames | raw mic PCM, 16 kHz s16le mono (100 ms = 3,200 B each), **only** between `start` and `stop`. Anything else binary is ignored and never stored. Max 8 s |
| client → server | `{"type":"voice","action":"stop"}` / `"cancel"` | released: transcribe / throw the audio away |
| server → client | `{"type":"voice","state":"listening"\|"heard"\|"error","text","command","ok","note"}` | what was heard and what it did |

`hud` frames carry **`voice`**: `{"state","text","note"}` or `null`. It is shown in place of the
header for ~2 s (4 s for `status`). Voice only **arms**: the same two-tap confirm sends. On the
reply screen, speech replaces the draft text.

**Session screens:** `ui.mode` can also be

```json
{"mode": "end", "coin": null, "summary": {...live numbers...}}
{"mode": "summary", "coin": null, "summary": {...}, "posted": null, "note": null}
```

`summary` = `{started_ms, ended_ms, duration_s, mode, trades, dry_runs, realized, unrealized,
session_pnl, max_drawdown, alerts_fired, alerts_acted, alerts_snoozed, policy_blocks}`.
- `back` (double tap) on the HUD now opens `end`. The plugin exits only on a double tap on `end`
  / `summary`, or while the link is down.
- `end`: `tap` ends the session and opens `summary`; `next` / `prev` go back to the HUD.
- `summary`: `tap` posts to `JOURNAL_CHAT_ID` (`posted` true/false plus `note`); a swipe goes to
  the HUD.

**Robustness (BUILD_LOG §26):**

| Field / frame | Meaning |
|---|---|
| `hello.heartbeat_ms` | now **1000**: a `hud` frame at least every second, so > 3 s of silence = STALE on the glasses |
| `hello.glyphs`, `hud.glyphs` | `"unicode"` (▲ ━ ●) or `"ascii"` (/!\ - *), from `GLYPHS` in `.env`. Missing = the Day-2 look |
| `hud.src_ms` | int ms \| null: backend wall clock when the exchange data in this frame arrived (`MarketState.last_change_ms`) |
| client → server `{"type":"rendered","seq":N,"src_ms":T}` | sent once per new `src_ms`, after `textContainerUpgrade` for that frame's text returned ok. The backend records `now − src_ms` → p50/p95 in `/status` and `/healthz → latency`. A malformed one → `bad_message` |

## Day 4: Away mode (still additive: `v` stays 2)

G2 plugins have no background / push path to the glasses (confirmed by the Even team). An alert
that fires while no client is connected goes to Telegram instead (one DM per alert episode; see
`risk.py`).

- **On connect**, if an alert episode is still open (not acted on, snoozed or resolved), the
  first `hud` frame after `hello` already carries the RISK screen:
  ```json
  {"mode": "menu", "kind": "alert", "coin": "ETH",
   "items": ["Move SL", "Close 50%", "Close all", "Snooze 5m"], "cursor": 0, ...,
   "alert": {"coin": "ETH", "side": "LONG", "size": 0.5, "liq_px": 2980.0,
             "liq_dist_pct": 1.32, "mark": 3020.0, "threshold_pct": 5.0,
             "entry": 3412.0, "leverage": 5.0, "upnl": 41.2}}
  ```
  - Its numbers come from the live position at connect time, not from when the alert fired.
  - With several open episodes, the most urgent one (smallest `liq_dist_pct`) is shown.
  - A forwarded draft waiting for a client is shown after the alert is left.
- `alert` gained `entry`, `leverage` and `upnl` (null when unknown). A client that ignores them is
  unaffected.
- **No new client messages.** Snoozing (the `Snooze 5m` item, or voice "snooze") or a fill / a
  placed stop on that position ends the episode on the backend. Backing out with a double tap
  doesn't, so the next connect shows the RISK screen again.
- `/healthz → alerts` gained `open` (the episodes), `dms_sent`, `dms_suppressed` and `away_mode`
  (no glasses connected).

## Endpoint and auth

```
wss://<backend-host>/ws?code=<PAIRING_CODE>
```

- `PAIRING_CODE` is the shared secret from the backend's `.env` (≥ 8 chars). Compared with
  `secrets.compare_digest`.
- Wrong or missing code → the server **accepts, then immediately closes with code `4401`**,
  reason `"bad pairing code"`. Clients must not retry-loop on 4401 (the plugin shows
  "Pairing code rejected" and waits for a new code or a FOREGROUND_ENTER).
- The code travels in the query string, so anything that logs full URLs sees it. The backend
  redacts it from its own logs (`code=***`); the local ngrok inspector (`:4040`) does not.
- Browsers can't add headers to a WebSocket handshake, so the `ngrok-skip-browser-warning`
  header cannot be sent here (it is sent on every HTTP `fetch`).

## Close codes

| Code | Sent by | Meaning | Client reaction |
|---|---|---|---|
| `4401` | server | bad pairing code | stop, ask for a new code |
| `1000` | client | deliberate close (reconnect / exit) | — |
| anything else / network drop | — | transient | reconnect with backoff 1 s → 2 s → … → 15 s |

## Server → client frames

### `hello` (once, right after a successful connect)

```json
{"type": "hello", "v": 2, "network": "testnet", "min_interval_ms": 500, "heartbeat_ms": 1000,
 "trading": {"enabled": false, "status": "off"}, "glyphs": "unicode"}
```

### `hud` (immediately after `hello`, then on change ≤ every 500 ms, plus a heartbeat every 5 s)

```json
{
  "type": "hud",
  "v": 2,
  "seq": 42,
  "ts": 1795432108660,
  "network": "testnet",
  "feed": {"connected": true, "age_ms": 420},
  "account": true,
  "view": {"index": 0, "count": 1},
  "position": {
    "coin": "ETH", "side": "LONG", "size": 0.5, "entry": 3412.0,
    "mark": 3500.0, "upnl": 41.2, "upnl_pct": 2.415, "roe_pct": 12.07,
    "liq_px": 2980.0, "liq_dist_pct": 14.86, "leverage": 5.0,
    "mid": 3494.4, "mark_source": "markPx", "leverage_type": "cross"
  },
  "ref": {"coin": "ETH", "mid": 3494.4, "mark": 3500.0, "mark_source": "markPx"},
  "ui": {"mode": "hud"},
  "trading": {"enabled": true, "status": "ready"}
}
```

| Field | Type | Meaning |
|---|---|---|
| `seq` | int | per-connection counter, +1 per `hud` frame (gaps never happen; use it to spot heartbeats) |
| `ts` | int ms | server wall clock when the frame was built |
| `feed.connected` | bool | backend ↔ Hyperliquid WebSocket is up |
| `feed.age_ms` | int \| null | ms since the backend last heard *anything* from Hyperliquid |
| `account` | bool | `HL_ACCOUNT_ADDRESS` is configured (false → price-only mode) |
| `view.index` / `view.count` | int | which position this client is looking at, of how many (per connection) |
| `position` | object \| null | the selected position, `null` when there are none. While a menu is open the client is **locked to that coin**; if it closes, `position` is `null` (never a different coin) |
| `ref` | object | reference coin (ETH) price — always present, shown when `position` is null |
| `ui` | object | which screen to draw — see below |
| `trading.enabled` | bool | an order executor is configured (`HL_ACCOUNT_ADDRESS` **and** `HL_AGENT_KEY` set) |
| `trading.status` | str | `off` / `warming` (loading coin metadata) / `ready` / `error` |

`position` fields:

| Field | Unit | Definition |
|---|---|---|
| `coin` | — | Hyperliquid coin name (`"ETH"`); the HUD appends `-PERP` |
| `side` | — | `"LONG"` if `szi > 0`, else `"SHORT"` |
| `size` | coin | `abs(szi)` |
| `entry` | USD | `entryPx` |
| `mid` | USD | latest mid (allMids / activeAssetCtx.midPx) |
| `mark` | USD | `activeAssetCtx.markPx`, or `mid` if no markPx yet |
| `mark_source` | — | `"markPx"` or `"mid"` (tells you which one `mark` is) |
| `upnl` | USD | `szi × (mid − entry)` |
| `upnl_pct` | % | `upnl / (size × entry) × 100`, on entry notional |
| `roe_pct` | % \| null | `upnl / (size × entry / leverage) × 100` = `upnl_pct × leverage`, return on **initial margin** (Hyperliquid's own `returnOnEquity` definition); null if leverage is 0/unknown |
| `liq_px` | USD \| null | `liquidationPx` from the exchange (null = none) |
| `liq_dist_pct` | % \| null | LONG `(mark − liq)/mark × 100`, SHORT `(liq − mark)/mark × 100`; ≤ 0 = at/through liq |
| `leverage` | x | `leverage.value` |
| `leverage_type` | — | `"cross"` / `"isolated"` |

Positions are ordered by `coin` so `next`/`prev` order is stable.

#### `ui` — the screen (backend `ActionFlow`, `actions.py`)

```json
{"mode": "hud"}

{"mode": "menu", "coin": "ETH", "items": ["Close 25%", "Close 50%", "Close 100%", "Back"],
 "cursor": 1, "trading": true, "note": null}

{"mode": "confirm", "coin": "ETH", "side": "LONG", "label": "Close 50%", "fraction": 0.5,
 "size": 0.25, "all": false, "exact": true, "est_px": 3500.0, "slippage_pct": 5.0,
 "expires_in_ms": 4200}

{"mode": "sending", ...same fields as confirm...}

{"mode": "result", "coin": "ETH", "ok": true, "status": "filled",
 "message": "Filled 0.25 @ 3499.1", "label": "Close 50%"}
```

| Field | Meaning |
|---|---|
| `menu.note` | why we're back in the menu: `"Confirm expired - not sent"`, `"Cancelled - not sent"`, or null |
| `menu.trading` | false → the plugin shows "Trading off: read-only"; picking a close shows an error, never a confirm |
| `confirm.size` | **exactly what will be sent** (rounded *down* to the coin's `szDecimals`); for `all: true` it's the live size at arming and the order closes the whole live position |
| `confirm.exact` | false → coin metadata isn't loaded yet, so the size is unrounded (shown with `~`); the executor rounds it down before sending |
| `confirm.est_px` | mark price when armed, display only; the order is an IOC limit at mid ± `slippage_pct` |
| `confirm.expires_in_ms` | time left for the second tap |
| `result.status` | `filled` / `resting` / `rejected` / `error` |

### `ack` (reply to every valid intent)

```json
{"type": "ack", "action": "next", "ok": true, "mode": "hud", "view": {"index": 1, "count": 3}}
{"type": "ack", "action": "tap", "ok": false, "mode": "hud", "view": {"index": 0, "count": 0},
 "note": "no open position"}
{"type": "ack", "action": "tap", "ok": true, "mode": "confirm", "view": {"index": 0, "count": 1}}
```

`mode` is the screen *after* the intent. `note` explains refusals and cancellations (`"no open
position"`, `"second tap too fast - tap again"`, `"confirm expired"`, `"cancelled"`, `"busy - order
in flight"`, or the order's error message). A fresh `hud` frame follows (500 ms throttle).

### `order` (once per confirmed close, when Hyperliquid answers)

```json
{"type": "order", "ok": true, "status": "filled", "message": "Filled 0.25 @ 3499.1",
 "coin": "ETH", "fraction": 0.5}
```

The same result is shown by the `hud` frame's `ui` (`mode: "result"`). Every order is also
written to SQLite `order_log`.

### `error`

```json
{"type": "error", "error": "bad_message", "raw": "<first 200 chars of what you sent>"}
```

The connection stays open.

## Client → server frames

### `intent`

```json
{"type": "intent", "action": "tap"}
```

| `action` | Plugin input | `hud` | `menu` | `confirm` | `sending` | `result` |
|---|---|---|---|---|---|---|
| `next` | swipe down | next position | cursor down | cancel → menu | ignored | → hud |
| `prev` | swipe up | previous position | cursor up | cancel → menu | ignored | → hud |
| `tap` | tap | open menu (needs a position) | Back → hud; Close N% → **arm** (confirm screen) | **send** (if ≥ 0.3 s and ≤ 5 s after arming) | ignored | → hud |
| `back` | double tap (not on the HUD) | — | → hud | cancel → hud | ignored | → hud |

Since Day 3 a double tap **on the HUD** is sent as `back` and opens `End session?`; the plugin
exits the app on a double tap on `end` / `summary`. When the link is down the plugin always treats
the screen as the HUD and a double tap exits at once, so you can always leave.

Anything else (unknown `type`/`action`, including v1's `"actions"`, non-JSON) → `error`
`bad_message`. Every valid intent is appended to `intent_log` (timestamp, action, client address).

**Only one path produces an order:** `tap` on a "Close N%" item (arm), then a second `tap` 0.3–5 s
later, with the position still open on the same side. A partial close sends at most the size that
was shown, capped at the live size. Every order is reduce-only (`trading.py`).

## Timing

| Knob | Value | Where |
|---|---|---|
| Min gap between `hud` frames | 500 ms (bursts coalesce, latest state wins) | `MIN_INTERVAL_S` in `ws.py`, or `create_app(min_interval_s=…)` |
| Heartbeat `hud` frame when nothing changes | 1 s (5 s before Day 3) | `HEARTBEAT_S` in `ws.py`, or `create_app(heartbeat_s=…)` |
| Confirm window (second tap) | 5 s | `CONFIRM_WINDOW_S` in `actions.py` |
| Minimum gap between the two taps | 0.3 s (a bounced tap is not a confirm) | `MIN_CONFIRM_GAP_S` in `actions.py` |
| Result screen → HUD | 8 s, or any input | `RESULT_HOLD_S` in `actions.py` |
| Order slippage cap (IOC limit) | 5 % from mid | `SLIPPAGE` in `trading.py` |
| Whole-order timeout | 25 s (then "No reply - CHECK positions") | `ORDER_TIMEOUT_S` in `trading.py` |
| Plugin watchdog (no frame → reconnect) | 8 s (15 s before Day 3) | `WATCHDOG_MS` in `plugin/src/link.ts` |
| Plugin reconnect backoff | 1 s doubling to 15 s | `BACKOFF_*` in `plugin/src/link.ts` |
| Plugin `STALE` line + `!` dot on the HUD | > 3 s without a frame, or prices > 3 s old | `STALE_MS` in `plugin/src/hud.ts` |
| Plugin re-render tick (clock, STALE counter) | 1 s | `CLOCK_TICK_MS` in `plugin/src/main.ts` |
| Push-to-talk max length | 8 s | `MAX_TALK_MS` in `plugin/src/voice.ts`, `MAX_VOICE_BYTES` in `ws.py` |
| Voice overlay on the header | 2 s (status: 4 s) | `ws.py` |

## Trying it by hand

With the backend running locally (`npm run backend:dev`), any WebSocket client works, e.g.
`npx wscat -c "ws://127.0.0.1:8000/ws?code=$PAIRING_CODE"` and then type
`{"type":"intent","action":"tap"}`. Don't paste the real code into shared terminals or chats.
