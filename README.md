# Tripwire

**A trading-session risk co-pilot for Even Realities G2 smart glasses.**
Tripwire puts your Hyperliquid **testnet** positions in the corner of your eye: size, entry,
live uPnL, ROE and how far you are from liquidation. You flip between positions with a swipe on
the ring or temple. A tap opens a menu that can **cut a position by 25 / 50 / 100 %** or **move its
stop-loss** (to breakeven, or 1 % tighter): every order reduces risk, passes a policy guard, and
is sent only after a second, deliberate tap.

When a position gets within 5 % of liquidation, the glasses show a **▲ RISK** screen with those
actions and a 5-minute snooze. In **Away mode** (the glasses aren't connected), the Telegram bot
DMs you the alert once, with a one-line position summary. When you open Tripwire on the glasses
again, it lands straight on that RISK screen until you act, snooze, or the danger passes. Forward a
trade idea like `BTC long 96.5k sl 95k tp 99k` to the bot and it appears on the glasses as a
draft, checked against hard limits ($250, 5x, stop-loss required, daily loss lock, kill switch).
Messages in your Telegram Business chats show up as one line on the HUD, and after a fill you can
send a short drafted reply with one tap.

**Hold** the temple and say *"close half"*, *"stop to breakeven"* or *"snooze"*. Speech is
transcribed **locally** (faster-whisper, no cloud) and only **arms** the order; the second tap
still sends it. A **double tap** ends the session and shows a summary (trades, realized and
session P&L, max drawdown, alerts, policy blocks), which one more tap posts to your own Telegram
journal channel. If no update arrives for 3 s, the glasses say **STALE**.

Built for the **Heads Up** hackathon: **Track 02 – Crypto & DeFi** (main) and **Track 01 –
Messaging & News**, plus **Best Telegram integration**.

```
TRIPWIRE            TESTNET ●    09:05
ETH-PERP LONG 0.50 @ 3,412  5x
uPnL +$41.20 (+2.4%)  ROE +12.1%
Liq 2,980  12.7% away
━━━━━━━━━━━━━━━━━━━━━━━━━━
tap: menu  swipe: next  hold: talk  2x: end
```

Tap, pick "Close 50%" (or hold and say "close half"), and tap again within 5 seconds:

```
MIC "close half"
CONFIRM  Close 50%  ETH-PERP LONG
SELL 0.25 ETH  reduce-only IOC
at ~3,494.4 mark, max 5% slippage
Tap again within 5s to SEND.
━━━━━━━━━━━━━━━━━━━━━━━━━━
tap: SEND  swipe/2x: cancel
```

> **Status: Day 5, build locked; testnet funds pending.** Everything below runs and is tested end to
> end in the simulator. The real-data parts are WATCH mode (any public testnet address) and
> DRY_RUN (really signed testnet orders that are never sent). The order and stop-loss flows are
> also shown on an in-memory fake exchange (`npm run backend:demo`). No order has been sent to
> Hyperliquid yet: that needs funds and an approved agent key. When they arrive,
> `npm run golive:check` and [docs/GO_LIVE.md](docs/GO_LIVE.md) take it live.
> Screenshots: [day 2](docs/screenshots/day2/), [day 3](docs/screenshots/day3/),
> [day 4](docs/screenshots/day4/), [day 5](docs/screenshots/day5/).

## Who it's for

**Active perps traders who hold leveraged positions while they are away from their screen**: in
a meeting, commuting, cooking, at the gym. The position keeps moving, and liquidation is a
number they can't watch. Tripwire keeps the few numbers that matter in view without a phone:
- size, uPnL and the distance to liquidation
- a risk alert that interrupts when that distance gets small
- the risk-reducing actions (cut, tighten the stop), each one tap and a confirm away

Tripwire doesn't pick trades or tell anyone what to do. It shows numbers you can check, and it
does only what you confirm.

## Why Telegram + glasses

- **The glasses can't be woken from outside.** Even Realities confirmed to us that plugins have
  **no background or push path to the G2**: a plugin only runs while it is open on the phone. A
  risk alert can't reach the glasses unless Tripwire is already open on them.
- **So Telegram is the always-on half, not a fallback.** In **Away mode** (no glasses
  connected), every new alert goes to your Telegram as one DM per alert episode: a one-line
  position summary and "Open Tripwire on your glasses to act." Opening Tripwire then lands
  straight on the ▲ RISK screen, where voice or a tap arms the fix and a second tap sends it.
- **Telegram is where traders already are.** Trade ideas get forwarded there, and desks and
  communities talk there. The same bot also takes forwarded trade ideas (shown on the glasses as
  drafts), answers `/status` and `/summary`, and posts your session journal.
- **The glasses are where acting is fastest.** No unlock, no app switching: look, hold to speak
  or tap, confirm.

## How it works

```
G2 glasses ◄─BLE─► Even App (phone) running plugin/ ──wss/https──► ngrok ──► backend/ (FastAPI)
                                                                              ├─► Hyperliquid TESTNET (WebSocket)
                                                                              ├─► Telegram Bot API (long polling)
                                                                              └─► SQLite (metadata only)
```

- **`plugin/`**: Even Hub plugin (Vite + TypeScript, `@evenrealities/even_hub_sdk` 0.0.16). It
  creates one full-screen text container and updates it in place with `textContainerUpgrade`.
  It holds no API keys.
- **`backend/`**: Python 3.12, FastAPI, uvicorn and SQLite. It streams Hyperliquid testnet data,
  normalises positions, and pushes a JSON `hud` frame to the glasses at most every 500 ms.
  Every third-party call happens here. The main modules:
  - `actions.py`: the screens and the two-tap confirm.
  - `orders.py` and `policy.py`: the single order path and its guard.
  - `exchanges/`: the Hyperliquid, fake and Bybit-demo adapters.
  - `risk.py`: liquidation alerts.
  - `drafts.py`: the forward parser.
  - `inbox.py` and `replies.py`: Telegram Business.
  - `voice.py` and `voice_grammar.py`: local speech-to-text and the command grammar.
  - `session.py`: the session summary and journal post.
  - `latency.py`: exchange-event-to-render latency.
  - `golive.py`: the go-live readiness check.
- **`docs/`**:
  - [architecture](docs/architecture.md) and the [WebSocket protocol](docs/ws-protocol.md)
  - the [build log](docs/BUILD_LOG.md): every decision and deviation, with where each knob lives
  - [GO_LIVE](docs/GO_LIVE.md): the first live testnet orders, step by step
  - [DEMO](docs/DEMO.md): the 2:45 video shot list, the pre-recording checklist, and
    `npm run demo:reset` between takes
  - [FALLBACK_BYBIT](docs/FALLBACK_BYBIT.md): a plan only, for switching exchanges in under 2 h
  - [SUBMISSION](docs/SUBMISSION.md): hackathon submission drafts and API feature requests

## Setup

You need **Node.js 20+** (22 recommended), **[uv](https://docs.astral.sh/uv/)** (it fetches
Python 3.12 for you) and **git**. For real glasses you also need **ngrok** with a free account
and the **Even App** on your phone (2.2.10+).

### Quick start: simulator + WATCH mode (Windows / PowerShell, about 10 minutes)

No exchange account, no ngrok and no glasses needed. This follows a real Hyperliquid testnet
trader read-only. **Measured on a fresh clone: 7 min 36 s from `git clone` to the live HUD**
(BUILD_LOG §33).

1. **Clone and install** (about 1 minute):
   ```powershell
   git clone https://github.com/Tejasv-Singh/tripwire.git
   cd tripwire
   npm run setup
   ```
   npm then reports "6 vulnerabilities". They are all in dev tooling (vitest, vite / esbuild);
   `npm --prefix plugin audit --omit=dev` finds 0 in what ships.
2. **Optional: a Telegram bot token** (2 minutes): in Telegram, @BotFather → `/newbot` → copy the
   token. See [Telegram bot](#telegram-bot-optional). Without one, everything except Telegram
   works.
3. **Make the two config files, a pairing code, and find an address to watch:**
   ```powershell
   Copy-Item .env.example .env
   Copy-Item plugin\.env.example plugin\.env.local
   uv --directory backend run python -c "import secrets; print(secrets.token_urlsafe(12))"
   npm run watch:find
   ```
   `watch:find` listens to testnet trades for 20 s and then lists addresses with open positions.
   Pick one that shows a `liq` price, so alerts can fire.
4. **Edit the two files** (`notepad .env`, `notepad plugin\.env.local`):
   - `.env`: `PAIRING_CODE=<the code>`, `WATCH_ADDRESS=<an address from the list>`, and
     optionally `TELEGRAM_BOT_TOKEN=<token>`.
   - `plugin\.env.local`: `VITE_PAIRING_CODE=<the same code>`.
     `VITE_BACKEND_URL=http://127.0.0.1:8000` is already the default.
5. **Run.** Use three terminals in the repo folder, and start them in this order:
   ```powershell
   npm run backend:dev
   ```
   ```powershell
   npm run plugin:dev
   ```
   ```powershell
   npm run sim
   ```
   Wait for Vite's `ready` line before `npm run sim`, because the simulator doesn't retry.
   - The glasses show `TRIPWIRE WATCH  TESTNET ●` and the watched position.
   - `http://127.0.0.1:8000/healthz` shows `hyperliquid.connected: true`, and
     `telegram.status: running` if you set a token.
   - On first start, the voice model (~145 MB) downloads in the background in about 30–40 s.
   - The simulator controls are under step 6 of the full setup below.
   - Want the order flows without an account? `npm run backend:demo` (see
     [Try it without an account](#try-it-without-an-account)).

### Full setup (real glasses, tunnel, trading)

1. **Clone and install everything:**
   ```sh
   git clone https://github.com/Tejasv-Singh/tripwire.git
   cd tripwire
   npm run setup          # uv sync (backend) + npm install (plugin)
   ```
2. **Configure the backend.** Copy `.env.example` to `.env` in the repo root
   (`Copy-Item .env.example .env`). Each variable has a one-line comment saying where to get it.
   - `PAIRING_CODE` is required. Generate one with
     `uv --directory backend run python -c "import secrets; print(secrets.token_urlsafe(12))"`.
     A plain `python` may not exist on Windows; uv always has one.
   - `HL_ACCOUNT_ADDRESS` is optional. Leave it empty to see only the ETH price.
   - `WATCH_ADDRESS` is optional. With no account set, it follows any public testnet address
     read-only (`npm run watch:find` lists some).
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID` are optional. With an empty token the bot is
     skipped. Without an owner id, nobody can use `/status`, `/kill`, `/threshold` or forwarded
     drafts, and alerts can't be DMed.
   - `HL_AGENT_KEY` is optional. `DRY_RUN=true` (the default) signs real testnet orders with a
     throwaway key and never sends them. See [Trading on testnet](#trading-on-testnet-optional).
   - `OPENROUTER_API_KEY` is optional. Without it, reply drafts use templates.
     `ANTHROPIC_API_KEY` is not used.
   - The policy limits (`MAX_OPEN_NOTIONAL_USD`, `MAX_LEVERAGE`, `DAILY_LOSS_LIMIT_USD`,
     `KILL_SWITCH`) and the alert settings (`ALERT_LIQ_PCT`, ...) have safe defaults.
   - `VOICE_ENABLED=true` (default). The first push-to-talk downloads the ~145 MB `base` model
     once into `backend/data/models`; after that, nothing leaves the laptop.
   - `JOURNAL_CHAT_ID` is optional: your channel for session summaries
     (see [Telegram bot](#telegram-bot-optional)).
   - `GLYPHS=unicode` (default) draws `▲ ━ ●`. Set `ascii` if a display shows boxes.
3. **Configure the plugin.** Copy `plugin/.env.example` to `plugin/.env.local`. Set
   `VITE_PAIRING_CODE` to the same value as `PAIRING_CODE`; it's only read in development builds.
   `VITE_BACKEND_URL` decides where the plugin connects:
   - **Simulator** on the same PC: `http://127.0.0.1:8000` (the default).
   - **Real glasses:** your HTTPS tunnel origin from step 4, e.g.
     `https://<your-domain>.ngrok-free.app`.
4. **Real glasses only: set up the tunnel once.** Install ngrok with
   `winget install --id Ngrok.Ngrok --exact --source winget` (or see
   [ngrok.com/download](https://ngrok.com/download)). Then **open a new terminal**, run
   `ngrok update` (winget ships 3.3.1, which predates the `--url` flag), and then
   `ngrok config add-authtoken <your token from dashboard.ngrok.com>`.
5. **Real glasses only: use your own tunnel host.** `npm run tunnel` starts ngrok on the author's
   reserved domain (`caucus-strategic-endorse.ngrok-free.dev`), which your ngrok account can't
   use.
   - Claim your free static domain on dashboard.ngrok.com, and put it in the `tunnel` script in
     `package.json` (`ngrok http --url=<your-domain> 8000`).
   - Put both `https://<your-domain>` and `wss://<your-domain>` in the `network.whitelist` of
     `plugin/app.json`.
   - Set `VITE_BACKEND_URL=https://<your-domain>` in `plugin/.env.local`.
6. **Run it.** Use one terminal per command, in this order:
   ```sh
   npm run backend:dev    # 1. backend on http://127.0.0.1:8000 (auto-reload)
   npm run tunnel         # 2. real glasses only: ngrok -> :8000
   npm run plugin:dev     # 3. Vite on http://localhost:5173 (wait for "ready")
   npm run sim            # 4. Even Hub simulator pointed at the Vite server
   ```
   The simulated glasses show the live ETH mark price, or the watched / own position. Prices change
   as testnet moves, and the clock in the header keeps ticking.
   - In the simulator, swipe for next/prev and click (tap) for the actions menu.
   - Long-press is push-to-talk: the simulator records from your PC's default microphone.
   - Double-click on the HUD opens `End session?`. A second double-click there exits, and
     double-click backs out of any other screen. With the backend unreachable, one double-click
     exits.
7. **Run on real glasses:** from `plugin/`, run `npx evenhub qr --url http://<your-LAN-IP>:5173`
   and scan the code with the Even App. The phone must be on the same Wi-Fi. To install it
   without a dev server, build a package (next section).

### Build the installable package (.ehpk)

```powershell
npm run pack
```

This type-checks, builds `plugin/dist/` with Vite, and packs it with `evenhub-cli` 0.2.0 into
**`plugin/tripwire.ehpk`** (~45 KB), checking the manifest. The output ends with `Stamped
tripwire.ehpk` and `min_app_version 2.2.10 (SDK 0.0.16)`. With `--json` it prints
`"warnings":[],"ok":true`.

- **The backend URL is baked in at build time** from `VITE_BACKEND_URL` in
  `plugin/.env.local`. For a package, that must be your **HTTPS tunnel origin**, and both its
  `https://` and `wss://` forms must be in `app.json`'s `network.whitelist`.
- The dev pairing code is **not** in production builds. On the phone, you enter it once on the
  plugin page.
- `plugin/app.json` holds the manifest:
  - name `Tripwire`, `package_id` `com.tejasvsingh.tripwire`
  - permissions: `network` (whitelist: the ngrok origin, https + wss) and `g2-microphone`
    (push-to-talk)
  - `min_app_version` 2.2.10, SDK 0.0.16
- `*.ehpk` is gitignored. Ship it as a GitHub release asset, or with
  `npx evenhub create tripwire.ehpk` / `npx evenhub upload` after `npx evenhub login` (run from
  `plugin/`). `npx evenhub pack ... --check` also asks Even's servers whether the package id is
  free, which needs that login.

**Checks:** `npm test` runs the backend pytest suite and the plugin vitest suite.
`npm run check` also runs ruff, strict mypy and tsc. `curl http://127.0.0.1:8000/healthz` shows
the feed, trading, policy, alert and bot status.

**`npm run demo:reset`** gives you a clean slate between demo takes, with the backend running.
- It clears open alerts and Away-mode DM cooldowns, re-arms everything, and sets the threshold
  back to `ALERT_LIQ_PCT`.
- It starts a fresh session (not stored) and sends the glasses back to the HUD.
- It never touches `.env`, the exchange or the kill switch.
- With the backend stopped, it only resets the saved threshold.
- It uses the pairing code, and refuses requests that come through the tunnel.
- For the demo backend: `uv --directory backend run python scripts/demo_reset.py --url
  http://127.0.0.1:8010`.

### Try it without an account

- **WATCH a real testnet position:** run `npm run watch:find`, put one of the listed addresses in
  `.env` as `WATCH_ADDRESS=0x...`, and restart the backend. The header says `TRIPWIRE WATCH`,
  positions are live, alerts work, and tapping says "watch only".
- **Demo with a fake exchange:** `npm run backend:demo` serves a fake 0.5 ETH long (with a stop)
  on live testnet prices at `http://127.0.0.1:8010`. Point a second Vite at it:
  ```powershell
  $env:VITE_BACKEND_URL='http://127.0.0.1:8010'; npm --prefix plugin run dev -- --port 5174
  ```
  then `node plugin/node_modules/@evenrealities/evenhub-simulator/bin/index.js http://localhost:5174`.
  The header says `TRIPWIRE DEMO`.
  - Demo-only, localhost-only routes:
    - `POST /demo/forward {"text": "BTC long 96.5k sl 95k tp 99k"}` (as if you'd forwarded it
      to the bot)
    - `POST /demo/threshold {"pct": 20}`
    - `POST /demo/business {"name": "@alice", "text": "still in?"}`
    - `POST /demo/voice {"wav_path": "C:/.../close_half.wav"}`: a 16 kHz mono WAV that
      **replaces** the next push-to-talk's audio (for PCs without a virtual mic)
    - `GET /demo/dms` and `GET /demo/journal`: what was sent to Telegram. The demo backend never
      polls, so it can run next to the real one. It **sends with the real token** if one is set;
      add `--no-telegram` to only record. Don't blank the token with
      `$env:TELEGRAM_BOT_TOKEN=''`, because in PowerShell that deletes the variable and `.env`
      wins.
  - WATCH mode: `npm run backend:demo -- --watch 0x...`. In PowerShell, `npm` swallows the
    `--`, so run `uv --directory backend run python scripts/demo.py --watch 0x...` instead.

### Trading on testnet (optional)

Closing from the glasses needs a **funded Hyperliquid testnet account** and an **API (agent)
wallet** that the account has approved. The agent can place orders for the account, but it can't
withdraw.

1. Open [app.hyperliquid-testnet.xyz](https://app.hyperliquid-testnet.xyz) with your main wallet
   and get testnet USDC from the faucet. Your account must be eligible for it.
2. On the **API** page, generate an API wallet and **authorize** it. Copy its private key once.
   It is the agent key, not your main wallet key.
3. In `.env`, set `HL_ACCOUNT_ADDRESS` to your **main** account address (not the agent's) and
   `HL_AGENT_KEY` to the agent's private key. Restart the backend.
   - Run **`npm run golive:check`**. It checks the keys, that the agent is authorized, testnet
     USDC > $20 and the testnet guard, and signs one $15 order (dry run) validated against the
     SDK.
   - It prints PASS / FAIL / SKIP and the exact next command.
   - [docs/GO_LIVE.md](docs/GO_LIVE.md) is the full live sequence.
4. **Dry run first.** With `DRY_RUN=true` (the default), the header says `TRIPWIRE DRY RUN`.
   Every confirmed order is built and signed exactly like a live one, logged, appended to
   `backend/data/dry_run_orders.jsonl`, and **not sent**. The result screen says `DRY RUN`.
5. **Then go live:** set `DRY_RUN=false` and restart. The log shows
   `Trading LIVE on testnet: agent key 0x1234...abcd`, and `/healthz` shows
   `trading.dry_run: false`.
6. Open a small position on the testnet site. It shows up on the glasses within a few seconds.
   Tap, swipe to `Close 25%`, tap, check the confirm screen, then tap again within 5 s.

On the glasses:

| Screen | Tap | Swipe | Double tap | Hold (push-to-talk) |
|---|---|---|---|---|
| HUD | open the actions menu | next / previous position | **End session?** | a voice command |
| Menu (actions / Move SL / RISK alert / forwarded draft) | choose the item | move the cursor | back to the HUD | a voice command |
| Confirm | **send** (between 0.3 s and 5 s after choosing) | cancel, back to the menu | cancel, back to the HUD | only "cancel" |
| Result | back to the HUD (also after 8 s) | same | same | — |
| Reply offer (after a fill) | send the reply | nothing | skip (also after 20 s) | dictate a new reply ("cancel" skips) |
| End session? (live numbers) | end it → summary | back to the HUD | exit the app | — |
| Session summary | post to the journal channel | HUD | exit the app | — |

**Voice commands** (hold, speak, release; up to 8 s):
- The commands: `close quarter / half / all`, `stop to breakeven`, `stop tighter`, `snooze`,
  `cancel`, `status`. Noisy transcripts are matched fuzzily ("clothes half" → close half).
  Anything else answers `not a command`.
- What was heard shows on the glasses for 2 s (`MIC "close half"`), then the normal confirm
  screen.
- Voice can't open positions and never sends by itself. In WATCH mode it answers
  `BLOCKED  watch only`.
- Hold, don't tap-then-hold. A long press right after a tap is ignored, because that gesture
  opens the OS menu.

- **Actions menu:** Close 25 % / 50 % / 100 % (reduce-only IOC at mid ± 5 %), or Move SL... →
  **SL to breakeven** (stop at entry) or **SL tighter 1 %** (the current stop moved 1 % of mark
  toward the price).
- The new stop is placed first, and the old one is cancelled after that.
- A stop that would fire at once, or that is looser than the current one, is refused with the
  reason.
- Every order goes through the **policy guard** (see Safety rules) and is written to the SQLite
  `order_log` table (`backend/data/tripwire.sqlite3`) along with the exchange's answer.

### Telegram bot (optional)

1. In Telegram, open **@BotFather** (blue check), send `/newbot`, give it a name (`Tripwire`)
   and a username ending in `bot`. Copy the token. Treat it like a password; `/revoke` replaces it.
2. `/mybots` → your bot → **Bot Settings** → **Mode Settings**: turn **Secretary Mode** on. This
   is BotFather's current name for what used to be "Business Mode". Leave the other modes off.
   Turning **Allow Groups** off is recommended, since Tripwire only works in private chats.
3. Put the token in `.env` as `TELEGRAM_BOT_TOKEN=…`, then restart the backend. Auto-reload only
   watches `backend/src`, not `.env`.
4. The backend log shows `telegram=starting`, then `Telegram bot running as @your_bot`.
   `/healthz` → `telegram.status` is `running`. Send the bot `/start`. The reply includes **your
   numeric user id**. Put it in `.env` as `TELEGRAM_OWNER_ID=...` and restart. Everything below
   is owner-only:
   - `/status`: positions, uPnL, trading mode, kill switch, alert threshold, Away mode on/off,
     and any open alerts.
   - `/kill` / `/unkill`: block / allow every order that adds exposure. Closes and tighter stops
     still work. The setting survives restarts; `KILL_SWITCH=true` in `.env` can't be undone from
     Telegram.
   - `/threshold 8`: liquidation alerts at 8 % (shows the current value without a number).
     **The value is saved and survives restarts.**
   - `/summary`: the running session so far (the same numbers as the glasses' summary screen).
   - `/status` ends with
     `Latency (HL event -> glasses render): p50 … ms, p95 … ms`, measured while the glasses are
     connected.
   - **Forward (or type) a trade idea**, e.g. `BTC long 96.5k sl 95k tp 99k`,
     `ETH short @3400 SL 3480 TP 3200 3x $200`. The bot answers with the parsed draft and the
     policy verdict, and the glasses show `FORWARDED - your instruction, not advice` with Place
     order / Discard (Discard only if blocked). Ambiguous text is refused with the reason
     (`which price is 95k? label it (sl / tp / @entry)`).
   - **Away mode (risk alerts):** while no glasses are connected, an alert arrives as a DM:
     ```
     ▲ RISK - Away mode
     ETH-PERP LONG 0.5 @ 3,412 5x | uPnL -$109.00 | liq 2,980: 3.4% from mark 3,085 (alert at 5%)
     Open Tripwire on your glasses to act.
     ```
     - **One DM per alert episode.** The episode stays open until you act on it (a fill or a new
       stop on that position; a dry run doesn't count), snooze it, or it resolves (the position
       closes, or its distance is back above threshold + 1 %).
     - Never two DMs for the same position within 10 minutes, and a `/threshold` change doesn't
       re-send.
     - **Opening Tripwire on the glasses lands on the ▲ RISK screen** of the most urgent open
       alert, with live numbers. A waiting forwarded draft shows after it.
   - **Journal channel** for session summaries:
     1. Create a channel (or group) of your own.
     2. Add the bot as an **admin**. The bot DMs you the line to paste:
        `JOURNAL_CHAT_ID=-100…`.
     3. Put it in `.env` and restart.
     4. Then double tap on the HUD → `End session?` → tap → tap posts the summary. The summary
        has numbers only, ending "Numbers only - not advice."
5. Connecting the bot to your other chats needs **Telegram Premium**:
   Settings → Telegram Business → Chatbots → `@your_bot` → pick the chats. The backend then logs
   `business_connection …`.
   - New messages show on the HUD as `TG 1 new @name: <first 30 characters>`.
   - After a real fill, the glasses offer a ≤ 60-character reply to the last chat: tap to send,
     double tap to skip. It's drafted by OpenRouter if `OPENROUTER_API_KEY` is set, otherwise
     from a template.
   - Without Premium, everything except these two works.

The bot connects in the background and never blocks or crashes the backend. On timeouts or
Telegram flood control (HTTP 429) it retries with growing gaps: 5 s, doubling, capped at 300 s,
and never less than Telegram's `retry_after`. Only a rejected token stops it for good.

### Troubleshooting

| Symptom | Fix |
|---|---|
| Simulator shows "localhost refused to connect" | Vite wasn't ready yet. Start `plugin:dev` first, wait for "ready", then `sim`. |
| HUD says "Pairing code rejected" | `VITE_PAIRING_CODE` (dev) or the code saved on the phone page doesn't match `PAIRING_CODE` in `.env`. |
| HUD says "Backend unreachable" | Check that the backend and `npm run tunnel` are running, then open `https://<host>/healthz`. |
| `/healthz` returns an HTML page through ngrok | That's ngrok's browser warning. The plugin sends `ngrok-skip-browser-warning` on every fetch. If the WebSocket gets HTML too, use a Cloudflare named tunnel instead. |
| Backend exits with `TestnetGuardError` | `HL_API_URL` is not exactly `https://api.hyperliquid-testnet.xyz`. That's on purpose. |
| Log says `Telegram bot not up yet (TimedOut …)` or `(RetryAfter …)` | Telegram is slow or throttling the bot. Leave the backend running, because it retries by itself. Every restart or manual test call makes another request and extends the throttle. A brand-new bot took about 18 minutes to get through (9 attempts) on the first day. If it's still failing after about 30 minutes, `/revoke` the token in BotFather and put the new one in `.env`. |
| Log shows `Telegram polling hiccup (NetworkError: httpx.ReadError …) - reconnecting automatically` | Harmless. Telegram dropped the long-poll connection and the library reconnects by itself. `/healthz` → `telegram.status` stays `running`. (Before Day 2 this printed as a 40-line `Exception happened while polling for updates` traceback.) |
| `/healthz` says `telegram: error … InvalidToken` | The token in `.env` is wrong (a missing character or an extra space). Fix it and restart. |
| Log repeats `Telegram polling hiccup (Conflict: terminated by other getUpdates request; make sure that only one bot instance is running)`, and the bot ignores a `.env` change | **Two backends are running with the same bot token**, and Telegram gives each message to only one of them. Usually an old `backend:dev` keeps running after its terminal tab was closed (Ctrl+C never reached it). List them with `Get-CimInstance Win32_Process \| Where-Object { $_.CommandLine -match 'uvicorn' } \| Select ProcessId, ParentProcessId, CreationDate`, stop the **older** tree with `taskkill /PID <oldest uv pid> /T /F`, and leave the current one running. The Conflict lines stop within about 30 s. |
| The bot answers `Not authorized.` | `TELEGRAM_OWNER_ID` is empty or isn't your id. `/start` tells you your id. Restart after editing `.env`. |
| The bot answers `Not an order I can draft: ...` | The text was ambiguous (two coins, an unlabelled price, both long and short ...). Label every price: `sl 95k`, `tp 99k`, `@96.5k`. |
| The glasses show `BLOCKED: ...` on a draft or instead of a confirm | The policy guard refused it, and the reason is the rule. Limits: $250 notional, 5x, a stop-loss on every opening order, the daily loss lock, the kill switch. Stops must be tighter than the current one and on the safe side of mark. |
| The result says `DRY RUN: ... signed, not sent` | `DRY_RUN=true` (the default). The signed payload is in `backend/data/dry_run_orders.jsonl`. Set `DRY_RUN=false` (with an agent key) to really send. |
| No risk alert although a position is close to liquidation | Alerts need markPx (positions past the 25th coin fall back to mid and are skipped) and fire once per approach. They re-arm after the distance is back above threshold + 1 %. Check `/healthz → alerts` and `/threshold`. |
| Only one Away-mode DM although the position kept getting closer | By design: one DM per alert episode, and none for the same position within 10 minutes. The alert stays open, and opening Tripwire on the glasses lands on its RISK screen. `/status` lists `Open alerts:`; `/healthz → alerts` shows `open`, `dms_sent`, `dms_suppressed` and `away_mode`. |
| Tripwire keeps opening on the RISK screen | An alert is still open: act on it (a fill or a new stop on that position), snooze it, or wait until the distance is back above threshold + 1 %. Backing out with a double tap doesn't end it. A dry run doesn't either, because nothing changed on the exchange. |
| Menu says `Trading off: read-only (no agent key)` | Set `HL_ACCOUNT_ADDRESS` and either `HL_AGENT_KEY` or `DRY_RUN=true`. Restart the backend after editing `.env`. |
| Backend won't start: `HL_AGENT_KEY must be empty or a 64-hex-char private key` | The value isn't a private key. Copy the agent's private key (64 hex characters, `0x` optional), not its address. |
| Result: `User or API Wallet ... does not exist` | The agent wallet isn't authorized for that account on **testnet**, or `HL_ACCOUNT_ADDRESS` is the agent's address instead of the main account's. |
| Result: `Order could not immediately match ...` | The IOC order found no liquidity within 5 % of mid, which is common on thin testnet books. Nothing was filled; try again. |
| Result: `No reply in 25s - CHECK positions` | Hyperliquid didn't answer in time. The order **may still have gone through**, so check the position before you try again. |
| `/healthz` shows `trading.status` = `error` | The backend couldn't load Hyperliquid's coin list at startup. `trading.detail` says why. The first close tries again. |
| `evenhub-simulator` not found | `npm run sim` calls the simulator's `bin/index.js` directly. If you run it yourself, `npm rebuild @evenrealities/evenhub-simulator` restores the link. |
| HUD shows `STALE: no update for 5s` and `!` instead of `●` | No frame from the backend for more than 3 s, although the socket looks open (phone asleep, network stall, backend frozen). From 8 s on it says `- reconnecting` and the plugin opens a fresh socket. `STALE: prices 5s old` means the backend is fine but its Hyperliquid feed is quiet. Check `/healthz → hyperliquid.last_msg_age_ms`. |
| Voice: `MIC  voice unavailable` / `VOICE_ENABLED=false` | Voice is off in `.env`, or the model failed to load (see `/healthz → voice.detail`). The first load needs internet once, for the ~145 MB download. |
| Voice: `MIC "..."` + `not a command` | It heard you but not a command. Say it as listed (`close half`, `stop to breakeven`...). Two commands at once are refused: "one command at a time". |
| Glasses show boxes instead of `▲ ━ ●` | Set `GLYPHS=ascii` in `.env` and restart. The fallback is `/!\ - *`. |
| Summary screen says `DRY RUN - printed to the backend log` | `JOURNAL_CHAT_ID` isn't set, so the summary was printed to the backend log (`JOURNAL DRY RUN ... would post:`) instead of posted. To post it, set `JOURNAL_CHAT_ID`, make the bot an **admin** of that channel, and restart. |
| Summary screen says `Not posted: <Telegram error>` | The bot couldn't post to `JOURNAL_CHAT_ID`. Usually it isn't an admin of that channel, or the id is wrong. |

## Safety rules

1. **Testnet only.** The backend refuses to start unless the Hyperliquid URL is exactly
   `hyperliquid.utils.constants.TESTNET_API_URL`. The check runs at startup and again inside the
   feed. There is no mainnet switch.
2. **Agent key only.** Orders are signed with a Hyperliquid **API/agent wallet** key
   (`HL_AGENT_KEY`), never your main wallet key. Agent wallets can't withdraw funds. Only the
   agent's public address is ever logged.
3. **Reduce-only by default, one guarded path for everything else.** The glasses' own menus only
   offer risk-reducing orders: reduce-only IOC closes, and stop-losses that may only get
   tighter. The only way to open a position is your own forwarded instruction. **Every** order
   goes through one policy guard (`policy.py`) via one path (`OrderService.submit`):
   - Closes and tighter stops are always allowed, even with the kill switch on.
   - Opening orders need a stop-loss, at most **$250** notional and **5x**. They're blocked by
     the **daily realized-loss lock** ($100, UTC day) and by the **kill switch**
     (`KILL_SWITCH=true` or `/kill`).
4. **Two-tap confirm.** The first tap shows the exact order (side, size, price, slippage or the
   stop move). Only a second tap 0.3–5 s later sends it, and only if the guard still agrees and
   the position is still open on the same side. The backend enforces this, so a buggy or
   replayed client can't skip it.
5. **No advice.** Tripwire shows numbers you can verify (position, uPnL, liquidation distance).
   It doesn't tell you what to trade. A forwarded draft is labelled `FORWARDED - your
   instruction, not advice`. Nothing here is financial advice.
6. **Keys stay on the backend.** The plugin never holds API keys. It only knows the backend URL
   and a pairing code, and the dev pairing code is stripped from production builds.
7. **Dry run by default.** With `DRY_RUN=true`, orders are really signed (a test proves the
   signature recovers for testnet) but never sent.

All seven rules are enforced in code:
- `config.py`: testnet guard, key validation.
- `exchanges/hyperliquid.py`: `assert_testnet` before every signature, dry run.
- `orders.py` and `policy.py`: one path, the guard.
- `actions.py`: two-tap confirm.
- `hud.ts`: numbers only.

Tests cover each one (`backend/tests/test_policy.py`, `test_actions.py`, `test_stop_loss.py`,
`test_trading.py`, `test_dry_run.py`, `test_app.py`).

## Privacy

- **Bot API only.** Tripwire uses the official Telegram Bot API through python-telegram-bot. It
  has no userbot, no MTProto client, and never logs into your Telegram account.
- **Only chats you connect.** Through Telegram Business (Settings → Business → Chatbots), you
  choose which chats the bot can see. Disconnecting removes its access.
- **No message retention.** Message text is never written to the database or the logs. The bot
  records metadata only: connection id, chat id, message id and length. The SQLite schema has no
  column for message text, and a test enforces that. Logs also redact the pairing code, the bot
  token and an OpenRouter key.
- **The HUD's `TG 1 new` line** keeps only the first 30 characters of a message, only in
  memory, only for 10 minutes. Forwarded drafts are parsed in memory and only metadata is logged.
- **Reply drafts** send OpenRouter only the trade facts ("closed half of my ETH long at about
  1,684.9"), never your chat's messages, names or ids. Without a key, nothing is sent anywhere.
- **Only the owner** (`TELEGRAM_OWNER_ID`) can see `/status` and `/summary`, change the kill
  switch or threshold, forward drafts, or receive alert DMs.
- **Voice stays on the laptop.** Audio is streamed only while you hold push-to-talk, transcribed
  locally by faster-whisper, and then discarded. It is never stored or sent anywhere. Logs keep
  the command label and the audio length, never dictated reply text.
- **Journal posts** contain numbers only (times, trades, P&L, counts), and only to the channel
  you set.

## API feature requests (for Even Realities)

What would make Tripwire better, in order. Raised with the Even team at the organizers' meeting.

1. **A background alert / push slot for plugins.** Let a plugin's backend wake the glasses with
   a short, rate-limited notification (one or two lines, plus "open plugin"), even when the
   plugin isn't open on the phone. Today the only way to reach a trader who isn't wearing an
   open plugin is another app (Tripwire uses a Telegram DM; see
   [Why Telegram + glasses](#why-telegram--glasses)). A system-level slot would let the RISK
   alert reach the lens directly.
2. **An OS-owned, trusted confirm sheet** for actions that move money or send messages. The OS
   would draw it and answer it with a gesture the plugin can't fake, so the user doesn't have to
   trust plugin-drawn text.
3. **Secure keystore storage** for small secrets (our pairing code currently sits in
   `setLocalStorage`).
4. **Dashboard widgets:** three live numbers at a glance, without opening the plugin.
5. **In-place list updates** (lists can only be rebuilt today) and **`eventSource` on every
   gesture** (today it's only on system events, and a tap arrives with `eventType: undefined`).

The details, plus the Hyperliquid and Telegram requests we hit while building, are in
[docs/SUBMISSION.md](docs/SUBMISSION.md#api-feature-requests).

## What existed before Nov 23

Before the hackathon (Nov 22–23, 2026) there were only **research notes and a product plan**.
There was **no code**. All code in this repository was written during the hackathon, starting
2026-11-23. The plugin was scaffolded from Even Realities' public `asr` template
(`npx degit even-realities/evenhub-templates/asr`), and `docs/BUILD_LOG.md` lists everything
that was changed from it.

### Open-source software used

| Library / tool | Version | License | Used for |
|---|---|---|---|
| [@evenrealities/even_hub_sdk](https://www.npmjs.com/package/@evenrealities/even_hub_sdk) | 0.0.16 | MIT | glasses bridge (containers, events) |
| [even-realities/evenhub-templates](https://github.com/even-realities/evenhub-templates) (`asr`) | HEAD on 2026-11-23 | see repo | plugin scaffold |
| [@evenrealities/evenhub-simulator](https://www.npmjs.com/package/@evenrealities/evenhub-simulator) | 0.9.5 | MIT | desktop glasses simulator |
| [@evenrealities/evenhub-cli](https://www.npmjs.com/package/@evenrealities/evenhub-cli) | 0.2.0 | proprietary (UNLICENSED), dev tool | `evenhub pack` / `qr` |
| [Vite](https://vitejs.dev) | 5.4 | MIT | plugin dev server and build |
| [TypeScript](https://www.typescriptlang.org) | 5.9 | Apache-2.0 | plugin language |
| [Vitest](https://vitest.dev) | 2.1 | MIT | plugin tests |
| [FastAPI](https://fastapi.tiangolo.com) | 0.165 | MIT | backend HTTP/WebSocket |
| [Uvicorn](https://www.uvicorn.org) | 0.54 | BSD-3-Clause | ASGI server |
| [Pydantic](https://docs.pydantic.dev) / pydantic-settings | 2.14 / 2.16 | MIT | validation, `.env` config |
| [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) | 0.24.0 | MIT | testnet constants; signed orders later |
| [websockets](https://websockets.readthedocs.io) | 17.4 | BSD-3-Clause | Hyperliquid WebSocket client |
| [python-telegram-bot](https://python-telegram-bot.org) | 22.8 | LGPL-3.0 | Telegram Bot API |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) / [CTranslate2](https://github.com/OpenNMT/CTranslate2) | 1.2.1 / 4.9.1 | MIT / MIT | local speech-to-text (push-to-talk) |
| [Whisper `base` model](https://huggingface.co/Systran/faster-whisper-base) (OpenAI Whisper, converted) | — | MIT | speech model, downloaded once |
| [pytest](https://pytest.org), [Ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org), [HTTPX](https://www.python-httpx.org) | 9.2 / 0.17 / 2.4 / 0.28 | MIT / MIT / MIT / BSD-3-Clause | tests, lint, types |
| [uv](https://docs.astral.sh/uv/) | — | MIT / Apache-2.0 | Python env and lockfile |
| [ngrok](https://ngrok.com) | — | proprietary service | HTTPS tunnel to the laptop |

## License

[MIT](LICENSE) © 2026 Tejasv Singh Hada
