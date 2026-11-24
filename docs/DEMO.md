# Demo video: 3-minute storyboard + pre-recording checklist

**Rule for the recording: everything on camera is real Hyperliquid TESTNET.** That means a real
testnet position, real testnet orders, and the real backend (`npm run backend:dev`). **Never** use
`scripts/demo.py` (the in-memory fake exchange) on camera. Its header says `TRIPWIRE DEMO`, which
gives it away, and it isn't what we claim.

The glasses header proves it in every shot:

| Header | Meaning | On camera? |
|---|---|---|
| `TRIPWIRE            TESTNET ●    14:02` | own account, live testnet, data fresh | **yes** |
| `TRIPWIRE DRY RUN    TESTNET ●` | signed, never sent | rehearsal only |
| `TRIPWIRE DEMO ...` | fake exchange | **never** |
| `TRIPWIRE WATCH ...` | someone else's account, read-only | only in the no-funds fallback at the end |

---

## Storyboard (3:00)

Three capture sources, cut together:
- **G**: the glasses: the simulator window, or on real G2 the plugin's phone page, which mirrors
  the glasses text.
- **P**: the phone with Telegram.
- **W**: the Hyperliquid testnet web UI (Positions / Open Orders / Trade History), as proof.

| Time | Shot | What happens | Voice-over (short, no advice) |
|---|---|---|---|
| 0:00–0:12 | G | HUD: `ETH-PERP LONG 0.05 @ …  5x` / uPnL / `Liq … 18.7% away`; the dot `●` after TESTNET | "Trading from a phone means looking away from what you're doing. Tripwire keeps the three numbers that matter in the corner of your eye." |
| 0:12–0:25 | G, then W | swipe → the next position (if two are open), swipe back. Cut to W: the same position, size and liq price | "This is a real position on Hyperliquid testnet. Same size, same liquidation price." |
| 0:25–0:55 | P → G → W | On P: `/threshold 20` (just above the liq distance on the HUD). G: the **▲ RISK** screen appears on its own → `Move SL` → `SL to breakeven` → confirm screen `SL none -> 3,412  reduce-only` → **tap** → `PLACED`. W: Open Orders shows the new stop | "When liquidation gets close, the glasses interrupt. Every order needs a second tap, and protective orders are reduce-only." |
| 0:55–1:20 | G → W | **Hold** the temple: `MIC  listening...` → say **"close a quarter"** → `MIC "close a quarter"` → `CONFIRM  Close 25%` → **tap** → `FILLED`. W: Trade History shows the fill | "Voice works hands-free and runs locally on the laptop. It only arms the order; the tap still sends it." |
| 1:20–1:50 | P → G → W | On P: forward a message `ETH long market sl 1630 $15 2x`. G: `FORWARDED - your instruction, not advice` / `ETH LONG $15 2x  market` / `SL 1,630` / `Policy OK` → `Place order` → tap → tap → `FILLED`. W: the new fill and its stop | "A trade idea from a chat becomes a draft with a stop attached. It's checked against your own limits, and it's your instruction, not advice." |
| 1:50–2:05 | P → G | Forward `ETH long market sl 1630 $1000 5x`. G: `BLOCKED: Notional $1,000 > max $250`; only `Discard` is offered. The message must have an SL, or the block reason is "Opening orders need a stop-loss (sl)" (also a fine shot) | "Over your own limit, there's nothing to send." |
| 2:05–2:25 | P | Take the glasses off (close the simulator). On P: `/threshold 25` → the **▲ RISK DM** arrives in Telegram. Then `/status`: positions, uPnL, `Latency (HL event -> glasses render): p50 … ms, p95 … ms` | "Glasses off? The same alert arrives as a Telegram message. Status shows how fast a Hyperliquid tick reaches the display." |
| 2:25–2:50 | G → P | Glasses back on. **Double tap** → `END SESSION?` (live trades and P&L) → tap → `SESSION 14:02-14:05` summary → tap → `Posted to journal`. P: the journal channel shows the post | "End the session and the summary goes to your own journal channel. Numbers only." |
| 2:50–3:00 | title card | `Hyperliquid testnet only · reduce-only by default · two-tap confirm · local voice · no advice` | "Tripwire: a risk co-pilot for your glasses." |

**Planned alternatives** (decide before recording, don't improvise):
- **The position is under water at 0:25:** `SL to breakeven` would sit above the mark, and the
  policy guard shows `BLOCKED: SL … is above mark … - it would fire at once`. Either film that as
  the guard working and then use `Close 50%` from the alert, or open the position earlier so it's
  in profit.
- **Voice misheard:** the screen shows what it heard and `not a command`. Hold and say it again.
  Never tap through a wrong confirm; a swipe cancels it.
- **Telegram Business reply** (needs Premium): after the 1:20 fill, `REPLY to @… on Telegram?` →
  tap. It's optional, so cut it if it isn't set up.

---

## Pre-recording checklist

### The day before
- [ ] Testnet funds have arrived: **≥ $50 USDC** in perps (spot USDC doesn't count; move it).
- [ ] `HL_ACCOUNT_ADDRESS` (main wallet) and `HL_AGENT_KEY` (API wallet) are in `.env`.
- [ ] `npm run golive:check` → **all PASS**.
- [ ] `docs/GO_LIVE.md` steps 3–7 done once. In particular, step 6 (SL to breakeven,
      `positionTpsl`) has been seen to work live.
- [ ] Full **rehearsal with `DRY_RUN=true`**: the whole storyboard. Every order says `DRY RUN`,
      nothing is sent, and the header says `TRIPWIRE DRY RUN`.
- [ ] Glyphs: `▲`, `━` and `●` render on the target display. If boxes appear, set
      `GLYPHS=ascii` and restart.

### 30 minutes before
- [ ] `DRY_RUN=false` in `.env`, then restart **the real backend** (`npm run backend:dev`). Only
      one backend instance per bot token; a second one gives 409 Conflict (README troubleshooting).
- [ ] `GET /healthz`:
  - `trading.exchange == "hyperliquid"`, `trading.dry_run == false`, `trading.status == "ready"`
  - `voice == "ready"` (the model is cached; the first load takes ~5 s)
  - `hyperliquid.connected == true`
- [ ] Telegram: `/unkill` (kill switch off). `/status` answers. `JOURNAL_CHAT_ID` is set, and
      the bot is an **admin** of the journal channel.
- [ ] **Open the demo position** in the testnet UI: ETH long, about $100 notional, 5x, **isolated**
      (so the liquidation price is near enough to read, around 18–20 %). Read the exact
      `Liq … % away` off the HUD, and pick the `/threshold` value for 0:25 just above it.
- [ ] Stop-losses: if you want `SL none -> …` on camera, cancel any existing stop in the UI first.
- [ ] Glasses link: plugin paired; the header shows `●`, and no `STALE` line for a minute.
- [ ] `/status` shows a latency line with samples (not "no samples yet").
- [ ] Have the forwarded messages ready in a chat: `ETH long market sl 1630 $15 2x` and
      `ETH long market sl 1630 $1000 5x`. Adjust 1630 to about 5 % below the current price; an SL
      above it is blocked as `sl_wrong_side`.
- [ ] Quiet room for the voice take. Test the mic in the simulator **with no position-changing
      command**: say "status".

### Privacy (in every frame)
- [ ] No `.env`, no terminal that has printed secrets, no ngrok inspector (`:4040`), no pairing
      code (the plugin's phone page has the code field: keep it collapsed).
- [ ] Telegram: hide your user id, phone number and other chats. The journal channel shows only
      the summary.
- [ ] Phone and laptop notifications off. Browser: only the testnet UI tab.
- [ ] The testnet wallet address is public by design, but you can still crop it.

### Right after recording
- [ ] `/threshold 5`. **The threshold is saved and survives restarts.**
- [ ] Cancel leftover stops; close or keep the testnet position deliberately.
- [ ] `DRY_RUN=true` again, then restart.
- [ ] Note in `docs/BUILD_LOG.md`: date, what was filmed, any retakes, and the latency p50/p95
      shown.

---

## If the testnet funds still haven't arrived

Don't fake it. Record the **WATCH** version:
- `WATCH_ADDRESS=<a real testnet trader>` (find one with `npm run watch:find`). The header says
  `TRIPWIRE WATCH`.
- The HUD, the RISK alert (via `/threshold`), the alert DM, voice ("close half" → `BLOCKED` /
  `Watch only - not your account, nothing sent`), the forwarded-draft policy check and the
  session summary all work on real data. Every order step ends in that `BLOCKED` screen, which
  is honest and on screen.
- Say it in the voice-over: "Following a public testnet account, read-only."
- The real-fill shots (0:25–1:50 on W) wait for a re-take once funds arrive.

The Bybit fallback (`docs/FALLBACK_BYBIT.md`) is a last resort for a dead Hyperliquid testnet,
not for this case.
