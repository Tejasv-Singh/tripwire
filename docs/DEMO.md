# Demo video: 2:45 shot list + pre-recording checklist

**Rule for the recording: everything on camera is real Hyperliquid TESTNET**: a real testnet
position (or a real watched one), real Telegram, and the real backend (`npm run backend:dev`).
**Never** use `scripts/demo.py` (the in-memory fake exchange) on camera. Its header says
`TRIPWIRE DEMO`, which gives it away, and it isn't what we claim.

The glasses header proves it in every shot:

| Header | Meaning | On camera? |
|---|---|---|
| `TRIPWIRE            TESTNET ●    14:02` | own account, live testnet, data fresh | **yes** |
| `TRIPWIRE WATCH      TESTNET ●` | a real testnet trader, read-only | yes, in the WATCH variant (say so) |
| `TRIPWIRE DRY RUN    TESTNET ●` | signed, never sent | rehearsal only |
| `TRIPWIRE DEMO ...` | fake exchange | **never** |

**OBS scene:** the simulator window **left**, a phone Telegram mirror **right**. A third source,
a "Glasses off" card, replaces the simulator during 0:25–0:55.

---

## Shot list (2:45)

The voiceover is recorded separately and laid over the edit. The **spoken command at 1:10 is
live** (the simulator's push-to-talk records the PC microphone).

| Time | On screen (left: simulator / right: Telegram) | Voiceover | Action you perform |
|---|---|---|---|
| **0:00–0:10** | Left: the HUD with the live position. Right: the bot chat. Title overlay "Tripwire" | **"Hold a leveraged position and step away from your screen, and the one number that matters, how close you are to liquidation, is the one you can't see."** | nothing (let it breathe) |
| 0:10–0:25 | Left: `TRIPWIRE  TESTNET ●` / `ETH-PERP LONG … @ …  5x` / `uPnL …  ROE …` / `Liq …  18.7% away` | "Tripwire puts it in the corner of your eye: your Hyperliquid testnet position, P&L, and the distance to liquidation, live." | swipe to the next position and back (only if two are open) |
| 0:25–0:40 | Left: **"Glasses off" card**. Right: `/status` → `Away mode: ON - no glasses connected, risk alerts come here` | "Glasses plugins can't be woken from outside. So when you take them off, Tripwire hands over to Telegram." | close the simulator window, switch OBS left to the card, then send `/status` |
| 0:40–0:55 | Right: `/threshold <N>` → `Alert threshold set to N%`, then **the DM arrives**: `▲ RISK - Away mode` / `ETH-PERP LONG … @ … 5x \| uPnL … \| liq …: 18.7% from mark … (alert at N%)` / `Open Tripwire on your glasses to act.` | "When liquidation gets close, you get one message, not a stream: the position, and one instruction." | send `/threshold <N>` (N from the checklist) and wait for the DM |
| 0:55–1:10 | Left: the simulator opens **straight on** `▲ RISK  ETH-PERP LONG …` / `Liq …  …% from mark (<N%)` / `> Move SL` / `Close 50%` / `Close all` / `Snooze 5m` | "Open Tripwire, and you land on the alert itself, with live numbers." | `npm run sim` (started just before; cut the load); switch OBS left back to the simulator |
| 1:10–1:30 | Left: `MIC  listening... release to send` → `MIC "stop to breakeven"` → `CONFIRM  SL to breakeven  ETH-PERP LONG` / `SL none -> 3,412  reduce-only` / `Tap again within 5s to SEND.` | "Say what you want. Speech is transcribed locally, on the laptop, and it only arms the order." | **hold** (long-press) the simulator touchpad, say **"stop to breakeven"**, release |
| 1:30–1:40 | Left: `SENDING` → `PLACED  SL to breakeven  ETH-PERP` / `SL set at 3412 …`. Optional 2 s insert: testnet UI, Open Orders | "Nothing moves until the second tap." | **tap within 5 s** of the confirm appearing |
| 1:40–2:00 | Right: forward `ETH long market sl <px> $15 2x` → the bot's draft reply. Left: `FORWARDED - your instruction, not advice` / `ETH LONG $15 2x  market` / `SL …` / `Policy OK` → `CONFIRM` → `FILLED` | "Forward a trade idea from any chat. It arrives as a draft with its stop, checked against your own limits. Your instruction, not advice." | forward the message; tap `Place order`, then tap again within 5 s |
| 2:00–2:10 | Right: forward `ETH long market sl <px> $1000 5x`. Left: `BLOCKED: Notional $1,000 > max $250`, with only `Discard` | "Over your limits, there is simply nothing to send." | forward it; tap `Discard` |
| 2:10–2:25 | Left: double tap → `END SESSION?` → tap → `SESSION …` summary → tap → `Posted to journal`. Right: switch to the journal channel; the post appears | "End the session, and the numbers go to your own journal channel." | double tap, tap, tap; switch the phone to the journal channel |
| **2:25–2:35** | Title card over both: `Testnet only · reduce-only by default · policy guard · two-tap confirm · no advice` | **"Testnet only, reduce-only by default, a policy guard on every order, and nothing is sent without a second tap. Numbers, never advice."** | nothing |
| **2:35–2:45** | Title card: **`Glance · Telegram · Two-tap confirm`** and the repo URL | **"Tripwire: glance at your glasses, hear from Telegram when you're away, and act with a two-tap confirm."** | nothing |

**WATCH variant** (no funds yet; header `TRIPWIRE WATCH`; add "following a public testnet
account, read-only" to the 0:10 line):
- 1:10–1:40: "stop to breakeven" ends in `BLOCKED` / `Watch only - not your account, nothing
  sent`. Show that, then say **"snooze"**: the screen returns to the HUD with
  `alert snoozed 5 min`, which ends the alert.
- 1:40–2:10: both drafts show `BLOCKED: Watch only - this is not your account`. Keep one
  10-second beat and use the time on the safety line.
- Everything else is unchanged, including the DM, landing on the RISK screen, and the journal
  post.

**Planned alternatives** (decide before recording, don't improvise):
- **Position under water at 1:10:** `SL to breakeven` would sit above the mark, and the guard
  shows `BLOCKED: SL … is above mark … - it would fire at once`. Say **"close half"** on the RISK
  screen instead (same two-tap confirm), or open the position earlier so it's in profit.
- **The DM doesn't come:** `/status` must say `Away mode: ON`. If the simulator is still
  connected, the alert goes to it instead. A previous take's alert or its 10-minute DM cooldown
  also holds it back: run `npm run demo:reset`.
- **Voice misheard:** the screen shows what it heard and `not a command`. Hold and say it again.
  Never tap through a wrong confirm; a swipe cancels it.
- **The confirm expired** (no tap within 5 s): `Confirm expired - not sent`. Pick the item again,
  or say it again.

---

## Pre-recording checklist

### The day before
- [ ] **Live position:** testnet funds arrived (**≥ $50 USDC** in perps), `HL_ACCOUNT_ADDRESS` +
      `HL_AGENT_KEY` in `.env`, `npm run golive:check` → **all PASS**, and `docs/GO_LIVE.md`
      steps 3–7 done once (step 6, `positionTpsl`, seen working).
      **Or WATCH:** `npm run watch:find`, and pick a `WATCH_ADDRESS` whose position shows a
      `liq` price (a `Liq none` position can't alert).
- [ ] Full rehearsal of the shot list. On a live account, use `DRY_RUN=true` first: every order
      says `DRY RUN` and nothing is sent.
- [ ] **Journal channel:** `JOURNAL_CHAT_ID` is set in `.env`, and the bot is an **admin** of
      that channel. With it unset, the summary is only a dry-run print.
- [ ] Glyphs `▲ ━ ●` render in the simulator (else `GLYPHS=ascii`).

### 30 minutes before
- [ ] **Backend: `npm run backend:dev`**, the real one: not `scripts/demo.py`, and never with
      `--no-telegram` (the DM and the journal post must really arrive). Only one backend per bot
      token, or Telegram returns 409 Conflict.
- [ ] Live account: `DRY_RUN=false` in `.env`, then restart. `/healthz`:
      `trading.exchange == "hyperliquid"`, `dry_run == false`, `status == "ready"`.
- [ ] **ngrok: `npm run tunnel`** (with your domain; README "Full setup" step 5). Needed for the
      phone page or real glasses. `https://<your-domain>/healthz` answers. The simulator itself
      talks to `127.0.0.1:8000`.
- [ ] `/healthz`: `hyperliquid.connected == true`, `voice.status == "ready"`,
      `telegram.status == "running"`. Telegram `/unkill` (kill switch off).
- [ ] **A threshold that triggers:** read `Liq … % away` off the HUD and set
      **N = that % rounded up + 2** (e.g. 18.7 % → `/threshold 21`). Write N down for 0:40.
      Below 6 % the default 5 % may already fire, which is fine, but then skip the `/threshold`
      beat.
- [ ] **Live position:** ETH long, about $100 notional, 5x, **isolated**, so the liquidation is
      18–20 % away. No stop-loss on it, so the confirm reads `SL none -> …`.
- [ ] The forwarded messages are ready in a chat: `ETH long market sl <px> $15 2x` and
      `ETH long market sl <px> $1000 5x`, with `<px>` about 5 % below the current price.
- [ ] **`npm run demo:reset`**: no open alert, no DM cooldown, threshold back to `ALERT_LIQ_PCT`,
      a fresh session, glasses back on the HUD. It never touches `.env` or positions.
- [ ] **OBS scene:**
  - left: the simulator window (`npm run sim`)
  - right: the phone's Telegram screen mirror (e.g. scrcpy for Android), with the bot chat open
    and the journal channel one tap away
  - the "Glasses off" card ready
  - audio: the PC microphone is the simulator's push-to-talk input (`npm run sim` records the
    default mic); keep the room quiet

### Between takes
1. `npm run demo:reset`, so the next take's alert DMs again at once.
2. Restart the simulator. On a live account, also:
   - cancel the stop the take placed, if you want `SL none -> …` again
   - close the $15 forwarded open, or accept the larger position

### Privacy (in every frame)
- [ ] No `.env`, no terminal that has printed secrets, no ngrok inspector (`:4040`), no pairing
      code field.
- [ ] Telegram: hide your user id, phone number and other chats. Phone notifications off, except
      the bot chat.
- [ ] The testnet wallet address is public by design, but you can still crop it.

### Right after recording
- [ ] `npm run demo:reset` (threshold back to `ALERT_LIQ_PCT`; it is saved and survives
      restarts).
- [ ] Live account: cancel leftover stops, close or keep the position deliberately, set
      `DRY_RUN=true` again, and restart.
- [ ] Note in `docs/BUILD_LOG.md`: date, live or WATCH, retakes, and the latency p50/p95 from
      `/status`.

The Bybit fallback (`docs/FALLBACK_BYBIT.md`) is a last resort for a dead Hyperliquid testnet,
not for missing funds. Missing funds means the WATCH variant.
