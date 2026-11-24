# Going live on Hyperliquid TESTNET

The first real orders, step by step: dry run first, then three live orders, from least risky to
most. Everything stays on **testnet**; the backend refuses any other URL. Do the steps in order
and stop at the first surprise. Each step says what to check **on the glasses** and **in the
Hyperliquid testnet UI** (<https://app.hyperliquid-testnet.xyz>).

> **Safety:** `/kill` in Telegram blocks every opening order at once, while closes and tighter
> stops keep working. `DRY_RUN=true` in `.env` (then restart) stops all sending.

## 0. Before you start

- [ ] Testnet USDC from the faucet. **More than $20**: a $15 order with margin to spare.
- [ ] Optional: keep a second backend in WATCH mode open. Don't: two backends with the same bot
      token fight over Telegram (README troubleshooting, "Conflict").

## 1. Keys

1. On the testnet site, connect your **main** wallet. Its address goes in `.env` as
   `HL_ACCOUNT_ADDRESS=0x...`.
2. **API** page → generate an API wallet → **Authorize**. Copy its **private key** once and put
   it in `.env` as `HL_AGENT_KEY=...`. This is the agent key: it can trade but can't withdraw.
   It never goes in chat, git or the plugin.
3. Leave `DRY_RUN=true`. Leave `WATCH_ADDRESS` empty: the own account wins over WATCH anyway.

## 2. `npm run golive:check`

```powershell
npm run golive:check
```

All five lines must say **PASS**:

| # | Check | If it fails |
|---|---|---|
| 1 | account + agent key in `.env` | fill in step 1 |
| 2 | the agent is authorized for that account (`/info userRole`, `extraAgents`) | authorize the API wallet for **this** account, or fix `HL_ACCOUNT_ADDRESS` (main address, not the agent's) |
| 3 | testnet USDC > $20 (perps + spot) | faucet; if most is in spot, transfer some to perps |
| 4 | testnet URL guard | `HL_API_URL` must be exactly `https://api.hyperliquid-testnet.xyz` |
| 5 | a $15 ETH market buy with a stop-loss signs and validates (dry run; nothing is sent) | read the problem list; don't go live |

## 3. Dry run on the glasses (`DRY_RUN=true`)

1. Start the stack: `npm run backend:dev`, `npm run tunnel`, `npm run plugin:dev`, then
   `npm run sim` or real glasses.
2. In the testnet UI, open a **small** position by hand: ETH, about $30, 3x cross, **long**.
3. **Glasses:** the header says `TRIPWIRE DRY RUN`. Within about 5 s the HUD shows
   `ETH-PERP LONG <size> @ <entry>  3x`.
   **UI (Positions tab):** the same size, entry and liquidation price. Liq on the glasses may
   differ by a rounding step.
4. Tap → Close 25% → tap → **tap again within 5 s**.
   **Glasses:** `DRY RUN  Close 25%  ETH-PERP` / `DRY RUN: sell 0.00xx ETH IOC @ ... signed, not
   sent`.
   **UI:** nothing changed: same size, no new trade.
   **File:** `backend/data/dry_run_orders.jsonl` has one new line with the signed order.

## 4. Go live: `DRY_RUN=false`

1. Set `DRY_RUN=false` in `.env` and restart the backend.
2. **Log:** `Trading LIVE on testnet: agent key 0x1234...abcd`. **Glasses:** the header has no
   `DRY RUN` tag. **`/status`:** `Trading: LIVE on testnet`.

## 5. Live order 1: Close 25 % (reduce-only IOC)

Tap → Close 25% → tap → tap again.

- **Glasses:** `FILLED  Close 25%  ETH-PERP` / `Filled 0.00xx @ <price>`. Within a few seconds
  the HUD size drops by a quarter.
- **UI:** the position is 25 % smaller. **Trade History** shows a sell marked *reduce only*
  near the mid. No open order is left behind (IOC).
- **Telegram:** if a business chat wrote in the last 30 min, the glasses offer a reply (2x
  skips).
- If it says `Order could not immediately match`, the testnet book was thin within 5 %. Nothing
  happened; try again.

## 6. Live order 2: SL to breakeven (the one to watch)

This is the payload not yet seen by the live exchange: a trigger order with grouping
**`positionTpsl`**, sized to the position.

1. The position must be **in profit** (long: mark above entry). Otherwise breakeven would fire at
   once and the guard blocks it with that reason. If it isn't, use "SL tighter 1%" on an existing
   stop instead, or wait.
2. Optional: first set a stop by hand in the UI (Positions → TP/SL) a few % below entry, so the
   "old SL cancelled" path runs too.
3. Tap → Move SL... → check `SL now <x>  entry <e>  mark <m>` → SL to breakeven → the confirm
   shows `SL <old> -> <entry>` → tap again.

What to check:
- **Glasses:** `PLACED  SL set at <entry>; old SL cancelled`, or `SL set at <entry>` without an
  old stop.
- **UI:** **Open Orders** has one **Stop Market**, reduce-only, trigger = your entry, size = the
  position, shown in the position's TP/SL column. Any old stop is gone.

**If it is rejected** (the glasses show `NOT DONE` with Hyperliquid's message), the old stop is
untouched, because the new one is always placed first. Note the exact message. Most likely fix:
the position-TP/SL size convention. We send the position size; the web UI is reported to send
`0` with `positionTpsl`. To change it, edit the `sz` passed to `_wire` in `set_sl`
(`backend/src/tripwire/exchanges/hyperliquid.py`), dry-run again (`npm run golive:check` and
step 3), and retry. Record the result in `docs/BUILD_LOG.md`.

The stop's limit price is the trigger −10 % (sell) / +10 % (buy). That's slippage room, as in the
SDK's own `basic_tpsl.py`, so the stop still fills if the price gaps through it.

## 7. Live order 3: a forwarded ~$15 open with a stop-loss

1. Forward or type to the bot: `ETH long market sl <about 3 % below mark> $15 2x`
   (e.g. `ETH long market sl 1630 $15 2x`).
2. **Telegram reply:** `FORWARDED - your instruction, not advice` / the parsed order /
   `Policy: OK - within limits, has a stop-loss` / `On your glasses now`.
3. **Glasses:** the draft screen → Place order → the confirm shows `BUY ~0.00xx ETH  $15 2x
   market` / `SL <x>  attached` → tap again → `FILLED`.
4. **UI:** the ETH position grows by about $15 at **2x cross** (leverage set first). **Open
   Orders** has its reduce-only Stop Market at your SL.
5. Check the guard once on purpose: forward `ETH long market sl 1630 $1000`. Expect
   `BLOCKED by policy: Notional $1,000 > max $250`, and nothing in the UI.

## 8. Wrap up

- Close what's left: tap → Close 100% → tap → tap. UI: flat, with no orders left (the
  `positionTpsl` stop goes with the position).
- `order_log` in `backend/data/tripwire.sqlite3` has one row per order with Hyperliquid's answer.
- Write what happened (especially step 6) into `docs/BUILD_LOG.md`.
- Going back to safe mode is one line: `DRY_RUN=true`, then restart.
