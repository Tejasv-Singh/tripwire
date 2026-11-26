# Tripwire: submission drafts (Heads Up hackathon)

Drafts to paste into the submission form. Numbers and claims come from `docs/BUILD_LOG.md`.

**Before submitting:**
- [ ] Add the demo video link (storyboard: [DEMO.md](DEMO.md)).
- [ ] Add the repo link (`https://github.com/Tejasv-Singh/tripwire`) and, if wanted, a GitHub
      release with `tripwire.ehpk` (`npm run pack`).
- [ ] If testnet funds arrived and `GO_LIVE.md` was run, change "no live order yet" below.

---

## Short description

**Tripwire: a trading-session risk co-pilot for Even Realities G2 glasses.**

**Problem.**
- Perps traders hold leveraged positions while they're away from their screen.
- Liquidation is the number that matters, and it's the one they can't watch.
- By the time they unlock a phone and find the app, the move is over.

**Who it's for.** Active perps traders who keep leveraged positions open while they're in a
meeting, commuting, or at the gym.

**How it works.**
- **The glasses show the position:** size, uPnL, ROE and the distance to liquidation, live from
  Hyperliquid testnet.
- **The ▲ RISK alert interrupts** when liquidation gets close. It offers only risk-reducing
  actions (cut 25/50/100 %, move the stop to breakeven or tighter).
- **Voice or a tap arms an action; a second tap sends it.** Voice is push-to-talk, transcribed
  locally.
- **Away mode.** The platform can't wake the glasses from outside, so when you aren't wearing
  them the same alert arrives once as a Telegram DM. Opening Tripwire then lands straight on the
  RISK screen.
- **Telegram is the other half.** Forward a trade idea to the bot and it appears on the glasses
  as a draft checked against your own limits. `/status`, `/summary`, `/threshold` and `/kill`
  work from any phone. A double tap ends the session and posts a summary to your journal
  channel.

**Safety.**
- Testnet only, behind a hard guard.
- Reduce-only by default, with one guarded order path (stop-loss required, $250, 5x, a daily
  loss lock, a kill switch) and a server-enforced two-tap confirm.
- An agent key only, with dry run by default.
- No advice: it shows numbers you can check and does only what you confirm.

**Tracks:** **Track 02 – Crypto & DeFi** (main) and **Track 01 – Messaging & News**. Also
entering **Best Telegram integration**.

### Longer version (for a "describe your project" field)

**Tripwire keeps the few numbers that matter in the corner of your eye, and the fixes one
confirmation away.**
- The G2 plugin draws a 9-line HUD and updates it in place: position, uPnL, ROE, and distance to
  liquidation, measured on mark price. In the simulator, a Hyperliquid tick reached the glasses
  in about 70–260 ms p50.
- A FastAPI backend streams Hyperliquid testnet over WebSocket. It owns every screen and the
  two-tap confirm; the plugin holds no keys and only draws what the backend says.
- Every order goes through one path and one policy guard:
  - reduce-only IOC closes
  - stops that can only tighten
  - forwarded opens, which need a stop-loss and must fit your limits

**Why Telegram + glasses.**
- The Even team confirmed that plugins have no background or push path to the glasses. So
  Telegram is the always-on half.
- **Away mode** DMs each new alert once per episode, with a one-line position summary and "Open
  Tripwire on your glasses to act." The alert persists until it is acted on, snoozed or
  resolved. Opening the plugin lands on its RISK screen, where voice ("stop to breakeven") arms
  the fix and a tap sends it.
- The bot also:
  - turns forwarded trade ideas into drafts on the glasses, with the policy verdict
  - answers `/status` (including render latency and Away mode) and `/summary`
  - posts the end-of-session journal (trades, realized and session P&L, max drawdown, alerts,
    policy blocks)
  - shows Telegram Business messages as a HUD line, and offers a ≤ 60-character reply after a
    fill (Business needs Premium)

**Privacy.** Bot API only. The bot sees only the chats you connect. Message text is never stored.
Voice is transcribed locally and discarded.

**Status.** Built and tested end to end in the simulator:
- 413 backend tests, 52 plugin tests
- WATCH mode on real testnet positions
- dry-run signed orders
- the order flows on a fake exchange

No live testnet order yet: the faucet funds were still pending. `npm run golive:check` and
`docs/GO_LIVE.md` are ready for when they arrive.

### Best Telegram integration: what the bot does

| Feature | Telegram API used |
|---|---|
| **Away-mode risk alerts**: one DM per alert episode while the glasses are off, then the plugin opens on the RISK screen | `sendMessage` to the owner |
| **Forwarded trade ideas → drafts on the glasses** (`BTC long 96.5k sl 95k tp 99k`), with the policy verdict in the reply | message / forward handlers (rule-based parser, no LLM) |
| `/status` (positions, uPnL, Away mode, open alerts, render latency p50/p95), `/summary`, `/threshold`, `/kill`, `/unkill`, all owner-only | commands |
| **Session journal**: the end-of-session summary is posted to your own channel | `sendMessage` to a channel where the bot is admin |
| **Journal setup helper**: add the bot to a channel and it DMs you the exact `JOURNAL_CHAT_ID=` line | `my_chat_member` updates |
| **Telegram Business inbox** on the HUD (`TG 1 new @alice: ...`, 30 characters, memory only), and a ≤ 60-character reply after a fill with one tap | `business_connection`, `business_message`, sending as the business account (needs Premium) |

Resilience: long polling with backoff that respects `RetryAfter`, a clean degraded mode without
a token, and owner-only commands. Bot API only: no userbot or MTProto.

---

## API feature requests

### Even Realities (Even Hub SDK / platform): top 5

1. **A background alert / push slot for plugins.** *(Confirmed missing by the Even team at the
   organizers' meeting.)*
   - Today a plugin runs only while it is open, so nothing can reach the lens when it isn't.
   - We work around it with a Telegram DM ("Away mode") and by opening straight on the RISK
     screen.
   - **Ask:** a rate-limited, user-granted permission for a plugin's backend to post a 1–2 line
     notification to the glasses with an "open plugin" action. Ideally it could carry a deep
     link to a screen (our `alert` id).
2. **An OS-owned, trusted confirm sheet.**
   - Our two-tap confirm is enforced by our backend but *drawn by the plugin*. The user has to
     trust that the text on screen is what will be sent.
   - **Ask:** `bridge.confirm({title, lines, action})`, drawn by the OS in its own chrome and
     answered with a gesture the plugin can't fake. It would return a one-time confirmation
     token the backend can check.
   - Money-moving and message-sending plugins would all use it.
3. **Secure keystore storage.**
   - The pairing code that authenticates the glasses to our backend is stored with
     `bridge.setLocalStorage`, as plain app storage.
   - **Ask:** a keystore API backed by the phone's secure storage (Keychain / Android Keystore)
     for small secrets and session tokens, so plugins don't have to choose between "type it
     every time" and "store it in the clear".
4. **Dashboard widgets.**
   - The most useful Tripwire screen is three numbers: position, uPnL, distance to liquidation.
   - **Ask:** a widget slot on the G2 dashboard that a plugin (or its backend) can update, so a
     glance works without opening the plugin. Combined with #1, a widget could turn red.
5. **In-place list updates and `eventSource` on every gesture.**
   - SDK 0.0.16 can update text (`textContainerUpgrade`) and images in place, but a list
     container's items can only be set by `createStartUpPageContainer` / `rebuildPageContainer`.
     So we draw every menu as text in one text container instead of a native list.
   - Input events are uneven:
     - `eventSource` (right temple / ring / left temple) exists only on system events
       (`Sys_ItemEvent`). Text- and list-container events (`Text_ItemEvent`, `List_ItemEvent`)
       don't say where the gesture came from.
     - A tap arrives with `eventType: undefined`, because `CLICK_EVENT` is `0` and protobuf omits
       zero-valued fields.
   - **Ask:**
     - a `listContainerUpgrade` (items, selection) that keeps focus
     - `eventSource` on every event type
     - an explicit `eventType` even for `CLICK`

Also hit, smaller:
- **A monospace font or a measure-text API.** The G2 font is proportional, so column alignment is
  approximate.
- **Document the tap-then-long-press OS menu**, or let a plugin claim long-press. Push-to-talk
  needed a 600 ms guard after a tap to avoid it.
- **Simulator:** a virtual-microphone or audio-file input, for testing push-to-talk on machines
  without a loopback device.

### Hyperliquid (from what we hit while building)

1. **Keep the WebSocket docs in sync with the API.** `webData2`, which our plan relied on for
   mark prices, is rejected on testnet ("Error parsing JSON into valid websocket request"). We
   switched to one `activeAssetCtx` subscription per coin.
2. **A server timestamp on `activeAssetCtx` (and `allMids`).**
   - Without it, "exchange event → glasses render" latency can only start from when the backend
     *received* the tick.
   - **Ask:** a `time` field, as `trades` already has.
3. **One subscription for many coins' mark prices.**
   - `allMids` has mids only (every ~5 s on testnet), and `markPx` needs one `activeAssetCtx` per
     coin.
   - For a watched market maker with 150+ positions we cap at 25 subscriptions and fall back to
     mid for the rest.
   - **Ask:** `allMarks`, or a multi-coin `activeAssetCtx`.
4. **Document `positionTpsl` stop-loss orders.** The size convention (position size vs `0`) and
   the trigger limit-price rules aren't in the Python SDK examples. We audited our payloads
   against `basic_tpsl.py` and `rounding.py`, but `positionTpsl` remains the first thing to
   watch live.
5. **Easier testnet funds for builders.** Faucet eligibility kept a funded testnet account (and
   so any live testnet order) out of reach for days. **Ask:** a builder or hackathon faucet path.
6. **Python SDK:** `WebsocketManager` can't route `clearinghouseState` subscriptions, so we wrote
   our own asyncio client.

### Telegram

1. **Business chat access without Premium**, or a free tier of it. Reading the trader's chats
   (with consent) and replying from their account needs Telegram Business, which needs Premium.
   Most traders we target don't have it, so the inbox line and one-tap replies are
   Premium-only.
2. **Gentler first-run limits for new bots.** A brand-new bot's `getMe` was throttled
   (`TimedOut`, then `RetryAfter`) for about 18 minutes on its first day. **Ask:** a documented,
   predictable limit for setup calls.
3. **Consistent naming.** BotFather now calls "Business Mode" **"Secretary Mode"**, while the
   docs and the app say Business. That cost setup time.

---

## What existed before Nov 23

- **Research notes and a product plan only. No code.** All code in the repository was written
  during the hackathon, starting **2026-11-23**. The git history starts there.
- The plugin was scaffolded from Even Realities' public `asr` template
  (`npx degit even-realities/evenhub-templates/asr`). `docs/BUILD_LOG.md` lists everything that
  was changed from it.

**Open-source software used:**

| Library / tool | Version | License | Used for |
|---|---|---|---|
| @evenrealities/even_hub_sdk | 0.0.16 | MIT | glasses bridge (containers, events, audio) |
| even-realities/evenhub-templates (`asr`) | HEAD on 2026-11-23 | see repo | plugin scaffold |
| @evenrealities/evenhub-simulator | 0.9.5 | MIT | desktop glasses simulator |
| @evenrealities/evenhub-cli | 0.2.0 | proprietary (UNLICENSED), dev tool | `evenhub pack` / `qr` |
| Vite / TypeScript / Vitest | 5.4 / 5.9 / 2.1 | MIT / Apache-2.0 / MIT | plugin build, language, tests |
| FastAPI / Uvicorn | 0.165 / 0.54 | MIT / BSD-3-Clause | backend HTTP + WebSocket |
| Pydantic / pydantic-settings | 2.14 / 2.16 | MIT | validation, `.env` config |
| hyperliquid-python-sdk | 0.24.0 | MIT | testnet constants, order signing |
| websockets | 17.4 | BSD-3-Clause | Hyperliquid WebSocket client |
| python-telegram-bot | 22.8 | LGPL-3.0 | Telegram Bot API |
| faster-whisper / CTranslate2 | 1.2.1 / 4.9.1 | MIT / MIT | local speech-to-text |
| Whisper `base` model (Systran/faster-whisper-base) | — | MIT | speech model, downloaded once |
| pytest / Ruff / mypy / HTTPX | 9.2 / 0.17 / 2.4 / 0.28 | MIT / MIT / MIT / BSD-3-Clause | tests, lint, types, HTTP |
| uv | — | MIT / Apache-2.0 | Python environment and lockfile |
| ngrok | — | proprietary service | HTTPS tunnel to the laptop |
