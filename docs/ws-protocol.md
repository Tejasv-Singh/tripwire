# Glasses ↔ backend WebSocket protocol (v1)

One WebSocket per glasses client. JSON text frames only. Source of truth:
`backend/src/tripwire/ws.py` + `hud.py` (server) and `plugin/src/protocol.ts` (client types).
Change both sides together and bump `v` on breaking changes.

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
{"type": "hello", "v": 1, "network": "testnet", "min_interval_ms": 500, "heartbeat_ms": 5000}
```

### `hud` (immediately after `hello`, then on change ≤ every 500 ms, plus a heartbeat every 5 s)

```json
{
  "type": "hud",
  "v": 1,
  "seq": 42,
  "ts": 1795432108660,
  "network": "testnet",
  "feed": {"connected": true, "age_ms": 420},
  "account": true,
  "view": {"index": 0, "count": 1},
  "position": {
    "coin": "ETH", "side": "LONG", "size": 0.5, "entry": 3412.0,
    "mark": 3500.0, "upnl": 41.2, "upnl_pct": 2.415,
    "liq_px": 2980.0, "liq_dist_pct": 14.86, "leverage": 5.0,
    "mid": 3494.4, "mark_source": "markPx", "leverage_type": "cross"
  },
  "ref": {"coin": "ETH", "mid": 3494.4, "mark": 3500.0, "mark_source": "markPx"}
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
| `position` | object \| null | the selected position, `null` when there are none |
| `ref` | object | reference coin (ETH) price — always present, shown when `position` is null |

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
| `upnl_pct` | % | `upnl / (size × entry) × 100` — on entry notional (ROE on margin comes Day 2) |
| `liq_px` | USD \| null | `liquidationPx` from the exchange (null = none) |
| `liq_dist_pct` | % \| null | LONG `(mark − liq)/mark × 100`, SHORT `(liq − mark)/mark × 100`; ≤ 0 = at/through liq |
| `leverage` | x | `leverage.value` |
| `leverage_type` | — | `"cross"` / `"isolated"` |

Positions are ordered by `coin` so `next`/`prev` order is stable.

### `ack` (reply to every valid intent)

```json
{"type": "ack", "action": "next", "ok": true, "view": {"index": 1, "count": 3}}
{"type": "ack", "action": "actions", "ok": true, "view": {"index": 0, "count": 1},
 "note": "actions menu not implemented yet (Day 1 logs only)"}
```

A fresh `hud` frame for the new selection follows (subject to the 500 ms throttle).

### `error`

```json
{"type": "error", "error": "bad_message", "raw": "<first 200 chars of what you sent>"}
```

The connection stays open.

## Client → server frames

### `intent`

```json
{"type": "intent", "action": "next"}
```

| `action` | Day-1 effect |
|---|---|
| `next` | select the next position (wraps) |
| `prev` | select the previous position (wraps) |
| `actions` | logged + written to SQLite `intent_log`; no trading yet |

Anything else (unknown `type`/`action`, non-JSON) → `error` `bad_message`. Every valid intent is
appended to `intent_log` (timestamp, action, client address).

## Timing

| Knob | Value | Where |
|---|---|---|
| Min gap between `hud` frames | 500 ms (bursts coalesce, latest state wins) | `MIN_INTERVAL_S` in `ws.py`, or `create_app(min_interval_s=…)` |
| Heartbeat `hud` frame when nothing changes | 5 s | `HEARTBEAT_S` in `ws.py`, or `create_app(heartbeat_s=…)` |
| Plugin watchdog (no frame → reconnect) | 15 s | `WATCHDOG_MS` in `plugin/src/link.ts` |
| Plugin reconnect backoff | 1 s doubling to 15 s | `BACKOFF_*` in `plugin/src/link.ts` |
| Plugin "link stale" warning on the HUD | 15 s | `STALE_FRAME_MS` in `plugin/src/hud.ts` |

## Trying it by hand

With the backend running locally (`npm run backend:dev`), any WebSocket client works, e.g.
`npx wscat -c "ws://127.0.0.1:8000/ws?code=$PAIRING_CODE"` and then type
`{"type":"intent","action":"next"}`. Don't paste the real code into shared terminals or chats.
