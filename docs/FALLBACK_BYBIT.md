# Fallback: Bybit (testnet or demo trading). Plan only, not implemented

**Status:** we are staying on Hyperliquid testnet. This file exists so that, *if* Hyperliquid
testnet becomes unusable (funds never arrive, testnet down for the demo), a switch takes **under
2 hours** and breaks none of the safety rules. Nothing below is wired in. Today
`exchanges/bybit_demo.py` is a stub that refuses every order.

Facts were checked against Bybit's v5 docs on 2026-11-25 (pages listed at the end). Anything
marked **check** is not stated in the docs and must be confirmed on the first live call.

---

## 0. Which Bybit environment

| | `BYBIT_ENV=testnet` (**default**) | `BYBIT_ENV=demo` |
|---|---|---|
| What it is | Bybit's separate test exchange | a paper account inside Bybit's main site |
| Matches our "testnet only" rule | **yes, literally** | no real money, but the infra and prices are mainnet's: needs an explicit opt-in |
| REST | `https://api-testnet.bybit.com` | `https://api-demo.bybit.com` |
| Private WS | `wss://stream-testnet.bybit.com/v5/private` | `wss://stream-demo.bybit.com/v5/private` |
| Public WS (prices) | `wss://stream-testnet.bybit.com/v5/public/linear` | `wss://stream.bybit.com/v5/public/linear`. Demo has **no** public stream; the docs say to use mainnet public data (read-only, no auth) |
| API key | create it on the testnet site | log in to the main site → switch to **Demo Trading** (its own user id) → avatar → API. A key made on the testnet site does not work for demo |
| Funds | test coins from the testnet site (**check** where in the UI) | `POST /v5/account/demo-apply-money` `{"adjustType":0,"utaDemoApplyMoney":[{"coin":"USDT","amountStr":"1000"}]}` (1 request per minute) |
| Prices | thin test markets | real market prices |

Recommendation: **testnet** by default, so the rule stays literal. Use `demo` only if testnet is
also unusable, and say so on camera ("Bybit demo account, paper money").

---

## 1. Env vars (add to `config.py` + `.env.example`)

```
EXCHANGE=hyperliquid          # hyperliquid | bybit   (default hyperliquid: nothing changes)
BYBIT_ENV=testnet             # testnet | demo
BYBIT_API_KEY=                # testnet or demo key ONLY (SecretStr)
BYBIT_API_SECRET=             # SecretStr; never logged (add it to RedactSecretsFilter)
BYBIT_RECV_WINDOW_MS=5000     # Bybit's default
```

`DRY_RUN`, `KILL_SWITCH`, `MAX_OPEN_NOTIONAL_USD`, `MAX_LEVERAGE`, `DAILY_LOSS_LIMIT_USD`,
`ALERT_*`, `PAIRING_CODE` and the Telegram vars keep their meaning unchanged. `WATCH_ADDRESS` is
**refused** with `EXCHANGE=bybit`, because Bybit positions are private. Config raises
`"WATCH mode is Hyperliquid-only"`.

Symbols are not configurable: coin `ETH` ↔ symbol `ETHUSDT`, `category=linear`, one-way mode
(`positionIdx=0`). Keep one `symbol_for(coin)` / `coin_for(symbol)` pair in the adapter.

---

## 2. The guard (do this first, ~10 min)

Same idea as `assert_testnet()`: an **exact-match allowlist**, checked in `create_app()` before
anything connects, so the process refuses to start otherwise.

```python
# config.py
BYBIT_ENDPOINTS = {
    "testnet": ("https://api-testnet.bybit.com",
                "wss://stream-testnet.bybit.com/v5/private",
                "wss://stream-testnet.bybit.com/v5/public/linear"),
    "demo":    ("https://api-demo.bybit.com",
                "wss://stream-demo.bybit.com/v5/private",
                "wss://stream.bybit.com/v5/public/linear"),   # public, unauthenticated only
}

def assert_bybit_nonlive(env: str, rest: str, private_ws: str) -> None:
    allowed = BYBIT_ENDPOINTS.get(env)
    if allowed is None or (rest, private_ws) != allowed[:2]:
        raise TestnetGuardError(f"Refusing Bybit {env!r} {rest!r}: only testnet/demo endpoints")
```

Rules the code must keep:
- URLs come **only** from `BYBIT_ENDPOINTS[BYBIT_ENV]`, never from free-form env vars. There is
  no `BYBIT_API_URL` to typo into `api.bybit.com`.
- **Auth headers and the WS `auth` op are only ever sent to the REST and private hosts in the
  allowlist.** The demo public socket (mainnet host) never gets a key: build it with a client
  that has no signer.
- **Startup key check:** `GET /v5/account/info` on the allowlisted REST host must return
  `retCode 0`. A mainnet key fails there, so a passing check also proves the key belongs to the
  test or demo account. On failure: trading `error`, with the detail
  `key rejected by <env> - is it a <env> key?`.
- The `/healthz` network field and the HUD header must say which venue this is (§6).
- Tests to copy from `test_app.py::test_refuses_to_start_on_mainnet`:
  - `api.bybit.com` refused
  - `api-demo.bybit.com` with `BYBIT_ENV=testnet` refused
  - unknown env refused
  - `WATCH_ADDRESS` + `EXCHANGE=bybit` refused

---

## 3. Signed REST client (`exchanges/bybit_client.py`, ~20 min)

`httpx.AsyncClient(base_url=<allowlisted REST>)`. Every private call sends these headers:

| Header | Value |
|---|---|
| `X-BAPI-API-KEY` | key |
| `X-BAPI-TIMESTAMP` | UTC ms (`server_time - recv_window <= ts < server_time + 1000`) |
| `X-BAPI-RECV-WINDOW` | `5000` |
| `X-BAPI-SIGN` | `hmac_sha256(secret, ts + key + recv_window + payload).hexdigest()` (lowercase hex) |

`payload` is the **query string** for GET (exactly as sent, e.g. `category=linear&symbol=ETHUSDT`)
and the **JSON body string** for POST. Serialise the body once and send that same string; don't
let httpx re-serialise it. Doc vector: ts `1658385579423`, key `XXXXXXXXXX`, recv `5000`, body
`{"category": "option"}` → signed string `1658385579423XXXXXXXXXX5000{"category": "option"}`.

Responses: `{"retCode": 0, "retMsg": "OK", "result": {...}, "time": ...}`. `retCode != 0` →
`OrderResult(False, "rejected", retMsg)`. Network errors → `"error"`. Reuse
`ORDER_TIMEOUT_S` (25 s) and `short_message()`.

**DRY_RUN:** build the full request, sign it, append `{"what", "env", "path", "body", "sign"}`
to `dry_run_orders.jsonl` + `dry_run_records`, and return `OrderResult(True, "dry_run", ...)`.
Same contract as `HyperliquidAdapter._record_dry_run`. Never send.

---

## 4. Market + position feed (`exchanges/bybit_feed.py`, ~35 min)

**Cheapest correct path:** translate Bybit messages into the **Hyperliquid shapes `MarketState`
already understands**. Then `normalize_positions`, the HUD, risk alerts, `/status`, the session
summary and the policy guard all work unchanged.

| Bybit source | → MarketState call |
|---|---|
| public `tickers.<SYMBOL>` (snapshot then deltas every 100 ms; **a missing field means unchanged**, so merge into a per-symbol dict) | `state.update_asset_ctx(coin, {"markPx": markPrice, "midPx": (bid1Price + ask1Price) / 2})` |
| REST `GET /v5/position/list?category=linear&settleCoin=USDT` once at start and after every reconnect (only size > 0 is returned), and private `position.linear` pushes afterwards | `state.update_clearinghouse({"assetPositions": [...]})` with the **full** current list (keep a dict by symbol; drop entries whose `side == ""` or size 0) |
| every message, any socket | `state.touch()`; `state.set_connected(True/False)` when the **public** socket opens or closes |

One position, Bybit → Hyperliquid shape:

```python
def to_hl_asset_position(p: dict) -> dict:
    size = float(p["size"])                                  # always positive on Bybit
    szi = size if p["side"] == "Buy" else -size              # "Sell" = short
    entry = p.get("avgPrice") or p.get("entryPrice")         # REST says avgPrice, WS entryPrice
    liq = p.get("liqPrice") or None                          # "" when out of range; cross = estimate
    return {"position": {
        "coin": coin_for(p["symbol"]), "szi": str(szi), "entryPx": entry,
        "liquidationPx": liq, "positionValue": p.get("positionValue"),
        "leverage": {"type": margin_type, "value": float(p["leverage"])},
    }}
```

`margin_type` comes from `GET /v5/account/info` (`marginMode`) at start: `ISOLATED_MARGIN` →
`isolated`, otherwise `cross`. Don't use `tradeMode`; it is deprecated and always `0`.

Sockets:
- Subscribe: `{"op":"subscribe","args":["tickers.ETHUSDT", ...]}`. Always include the ref coin
  (ETH) and every position's symbol; re-sync on position changes, like
  `HyperliquidFeed._sync_ctx_subscriptions`.
- Private auth: `{"op":"auth","args":[key, expires_ms, hmac_sha256(secret, f"GET/realtime{expires_ms}")]}`
  with `expires_ms = now + 10_000`. Wait for `{"op":"auth","success":true}`, then
  `{"op":"subscribe","args":["position.linear"]}`. `position` and `position.linear` can't be
  mixed in one request.
- Heartbeat: `{"op":"ping"}` every **20 s** on both sockets. Bybit drops an idle socket after
  10 min.
- Reconnect with the same backoff as `HyperliquidFeed` (1 s → 15 s), and re-pull the position
  list after every reconnect.
- Note: Bybit pushes a `position` message on every order create, amend or cancel, even with no
  change. The `!=` check in `_recompute_positions` already absorbs that.

---

## 5. The adapter: `BybitAdapter` replaces the stub (~30 min)

Implements `exchanges/base.py ExchangeAdapter` (`name = "bybit"`). Every order still goes through
`OrderService.submit()` → `PolicyGuard`. The two-tap confirm, the kill switch and the limits don't
change.

| Method | Bybit call | Notes |
|---|---|---|
| `start()` | `GET /v5/market/instruments-info?category=linear&limit=1000` (follow `cursor`), then `GET /v5/account/info` | cache per symbol: `priceFilter.tickSize`, `lotSizeFilter.qtyStep`, `.minOrderQty`, `.minNotionalValue`, `leverageFilter.maxLeverage`. Status `warming` → `ready` / `error` |
| `size_decimals(coin)` | from `qtyStep` | decimals of the step string (`"0.01"` → 2). **A step that isn't a power of ten (`"5"`) must round to a multiple of the step**, see rounding below |
| `close_reduce_only(o)` | `POST /v5/order/create` `{"category":"linear","symbol":S,"side":<opposite>,"orderType":"Limit","timeInForce":"IOC","price":P,"qty":Q,"reduceOnly":true,"positionIdx":0,"orderLinkId":"tw-<uuid12>"}` | opposite = `Sell` to close a long, `Buy` for a short. `P` = mid ∓ `SLIPPAGE` (5 %) rounded to `tickSize`, i.e. our IOC-limit-with-cap, same as Hyperliquid. `Q` = `o.size` rounded **down** to `qtyStep`, capped at the live size. `reduceOnly` makes TP/SL impossible on this order, which is correct |
| ↳ result | `GET /v5/order/realtime?category=linear&orderLinkId=…` (poll ~250 ms, up to `ORDER_TIMEOUT_S`) | create only **acknowledges** (async). Map `orderStatus`: `Filled` → `filled` with `avgPrice` / `cumExecQty`; `PartiallyFilledCanceled` → `filled` with the partial qty in the message; `Cancelled` / `Rejected` → `rejected` with `rejectReason`. **Check** these enum names against `/docs/v5/enum#orderstatus` |
| `set_sl(o)` | `POST /v5/position/trading-stop` `{"category":"linear","symbol":S,"tpslMode":"Full","positionIdx":0,"stopLoss":"<px>","slTriggerBy":"MarkPrice","slOrderType":"Market"}` | **one call replaces the position's stop**: no place-new-then-cancel-old step, and no 10 % limit slippage (Full mode = market). The confirm screen's line `new SL placed first, then old cancelled` should read `replaces the position stop` when `adapter.name == "bybit"`. Response `result` is `{}`: success = `retCode 0` → `placed` |
| `current_stop(coin)` | `GET /v5/position/list?category=linear&symbol=S` → `stopLoss` | `""` or `"0"` → `None`. `StopInfo(coin, oid=0, trigger_px)`: Bybit position stops have no order id, and nothing calls cancel by oid on this path |
| `place_order(o)` | `POST /v5/position/set-leverage` `{"category":"linear","symbol":S,"buyLeverage":"2","sellLeverage":"2"}`, then `POST /v5/order/create` `{"side":"Buy"/"Sell","orderType":"Market"` or `"Limit"`+`price`, `"qty":Q,"stopLoss":…,"takeProfit":…,"tpslMode":"Full","slTriggerBy":"MarkPrice","tpTriggerBy":"MarkPrice","positionIdx":0}` | set-leverage answers "leverage not modified" when it is already set; treat that as OK (**check** the exact retCode, probably 110043). `Q` = notional / price rounded down to `qtyStep`; refuse below `minOrderQty` or when `Q × px < minNotionalValue` with a clear `rejected`, before sending. Market orders are always IOC on Bybit; limits are GTC. This stays the only non-reduce-only order, reachable only from a forwarded draft after the policy check |
| `realized_pnl_today()` | `GET /v5/position/closed-pnl?category=linear&startTime=<00:00 UTC ms>&endTime=<now>&limit=100` (follow `cursor`) | sum `closedPnl`. **Check:** the docs don't say whether `closedPnl` is net of fees (`openFee` / `closeFee` are separate fields). Compare once against a known close; Hyperliquid's version uses `closedPnl − fee`. The window may be at most 7 days, which is fine for a day or a session |
| `realized_pnl_since(ms)` | same endpoint, `startTime = ms` | session summary |
| `stream_positions()` | `positions_stream(state)` | the feed (§4) keeps `MarketState` current, as with Hyperliquid |

**Rounding** (`trading.py`, new and next to the Hyperliquid rules, which stay):

```python
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

def round_to_step(value: float, step: str, mode=ROUND_DOWN) -> str:
    q = (Decimal(str(value)) / Decimal(step)).to_integral_value(rounding=mode) * Decimal(step)
    return format(q.normalize(), "f")            # strings, as Bybit expects

qty   = round_to_step(size, qty_step)                        # sizes: always DOWN
price = round_to_step(px, tick_size, ROUND_HALF_UP)          # prices: nearest tick
```

Hyperliquid's 5-significant-figure rule does **not** apply on Bybit; ticks are absolute.

---

## 6. Wiring (`main.py` + plugin, ~10 min)

- `create_app()`: when `settings.exchange == "bybit"`:
  - `assert_bybit_nonlive(...)`
  - `adapter = BybitAdapter(state, ...)`
  - `feed = BybitFeed(...)` **instead of** `HyperliquidFeed`
  
  `assert_testnet(settings.hl_api_url)` stays: the Hyperliquid URL is still guarded even when
  unused. `make_adapter` (tests, `scripts/demo.py`) is untouched.
- Network label: `hud.py NETWORK`, `/healthz`, `hello` and the startup log say `"testnet"` today.
  Make it `settings.network_label`: `testnet` / `bybit-testnet` / `bybit-demo`.
  - In `plugin/src/hud.ts` `headerLine()`, the literal `'TESTNET'` becomes:
    `frame.network === 'testnet' ? 'TESTNET' : frame.network.toUpperCase()`
  - So the glasses say `BYBIT-DEMO`. Keep it ≤ 12 characters.
- Telegram `/status`: add `exchange: bybit (testnet|demo)` to the first line.
- `trading.exchange` in frames: `bybit`. `headerTag()` needs no change.

---

## 7. What stays Hyperliquid-only (out of the 2 hours)

- `npm run golive:check` (`golive.py`): agent roles, `/info` funds, SDK wire validation. A Bybit
  branch would check the key (`/v5/account/info`), `GET /v5/account/wallet-balance?accountType=UNIFIED`
  `totalEquity > 20` and the guard. Add it only if the switch actually happens.
- WATCH mode and `npm run watch:find`: Hyperliquid positions are public; Bybit's are not.
- `docs/GO_LIVE.md` step 6 (`positionTpsl`): on Bybit, `trading-stop` replaces the stop by design.

---

## 8. Time budget and order of work

| # | Step | Min | Done when |
|---|---|---|---|
| 1 | Env vars + guard + refusal tests (§1–2) | 15 | `pytest -k bybit_guard` green; `api.bybit.com` refused |
| 2 | Signed client + doc-vector test + DRY_RUN record (§3) | 20 | signature test passes; a dry-run close writes a jsonl line |
| 3 | Rounding helpers + tests (§5) | 10 | step `"0.01"`, `"5"`, tick `"0.05"` cases |
| 4 | Feed: tickers + position list + private WS → `MarketState` (§4), tests with recorded JSON | 35 | the HUD shows a Bybit position in the simulator |
| 5 | Adapter methods with `httpx.MockTransport` tests (§5) | 30 | close / SL / open / PnL map to the right `OrderResult` |
| 6 | Wiring + header label (§6), then a live smoke test: `DRY_RUN=true` close → `DRY_RUN=false` Close 25 % → SL to breakeven | 10 | fills visible in the Bybit testnet / demo UI |
| | **Total** | **120** | |

Every existing test must still pass: `EXCHANGE` defaults to `hyperliquid`, and the fake adapter
and the demo backend don't change.

## Sources (Bybit v5 docs, read 2026-11-25)

`/docs/v5/demo`, `/docs/v5/guide` (auth), `/docs/v5/ws/connect`, `/docs/v5/order/create-order`,
`/docs/v5/position/trading-stop`, `/docs/v5/position`, `/docs/v5/position/close-pnl`,
`/docs/v5/market/instrument`, `/docs/v5/websocket/public/ticker`,
`/docs/v5/websocket/private/position`, all under `https://bybit-exchange.github.io`. Not yet
read: `/docs/v5/enum` (orderStatus, triggerBy) and `/docs/v5/rate-limit`.
