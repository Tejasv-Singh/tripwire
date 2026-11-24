# Glasses ↔ backend WebSocket protocol (v2)

One WebSocket per glasses client. JSON text frames only. Source of truth:
`backend/src/tripwire/ws.py` + `hud.py` + `actions.py` (server) and `plugin/src/protocol.ts`
(client types). Change both sides together and bump `v` on breaking changes.

**v2 (Day 2) vs v1:** the `actions` intent is gone; there are now `tap` and `back`. Every `hud`
frame carries `ui` (which screen to draw: HUD, menu, confirm, sending or result) and `trading`.
`position` has `roe_pct`. `ack` has `mode` and can be `ok: false`. There's a new `order` frame. The
backend owns the menu and the two-tap confirm; the plugin only draws what `ui` says.

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
{"type": "hello", "v": 2, "network": "testnet", "min_interval_ms": 500, "heartbeat_ms": 5000,
 "trading": {"enabled": false, "status": "off"}}
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

A double tap **on the HUD** never reaches the backend: the plugin exits the app. When the link is
down the plugin always treats the screen as the HUD, so a double tap can always exit.

Anything else (unknown `type`/`action`, including v1's `"actions"`, non-JSON) → `error`
`bad_message`. Every valid intent is appended to `intent_log` (timestamp, action, client address).

**Only one path produces an order:** `tap` on a "Close N%" item (arm), then a second `tap` 0.3–5 s
later, with the position still open on the same side. A partial close sends at most the size that
was shown, capped at the live size. Every order is reduce-only (`trading.py`).

## Timing

| Knob | Value | Where |
|---|---|---|
| Min gap between `hud` frames | 500 ms (bursts coalesce, latest state wins) | `MIN_INTERVAL_S` in `ws.py`, or `create_app(min_interval_s=…)` |
| Heartbeat `hud` frame when nothing changes | 5 s | `HEARTBEAT_S` in `ws.py`, or `create_app(heartbeat_s=…)` |
| Confirm window (second tap) | 5 s | `CONFIRM_WINDOW_S` in `actions.py` |
| Minimum gap between the two taps | 0.3 s (a bounced tap is not a confirm) | `MIN_CONFIRM_GAP_S` in `actions.py` |
| Result screen → HUD | 8 s, or any input | `RESULT_HOLD_S` in `actions.py` |
| Order slippage cap (IOC limit) | 5 % from mid | `SLIPPAGE` in `trading.py` |
| Whole-order timeout | 25 s (then "No reply - CHECK positions") | `ORDER_TIMEOUT_S` in `trading.py` |
| Plugin watchdog (no frame → reconnect) | 15 s | `WATCHDOG_MS` in `plugin/src/link.ts` |
| Plugin reconnect backoff | 1 s doubling to 15 s | `BACKOFF_*` in `plugin/src/link.ts` |
| Plugin "link stale" warning on the HUD | 15 s | `STALE_FRAME_MS` in `plugin/src/hud.ts` |

## Trying it by hand

With the backend running locally (`npm run backend:dev`), any WebSocket client works, e.g.
`npx wscat -c "ws://127.0.0.1:8000/ws?code=$PAIRING_CODE"` and then type
`{"type":"intent","action":"tap"}`. Don't paste the real code into shared terminals or chats.
