# Tripwire

**A trading-session risk co-pilot for Even Realities G2 smart glasses.**
Tripwire puts your Hyperliquid **testnet** positions in the corner of your eye: size, entry,
live uPnL, ROE and how far you are from liquidation. You flip between positions with a swipe on
the ring or temple. A tap opens a menu that can **cut a position by 25 / 50 / 100 %**: reduce-only,
and only after a second, deliberate tap. A Telegram Business bot handles messages and, later,
alerts.

Built for the **Heads Up** hackathon. Tracks: **Crypto & DeFi** and **Messaging**.

```
TRIPWIRE            TESTNET      09:05
ETH-PERP LONG 0.50 @ 3,412  5x
uPnL +$41.20 (+2.4%)  ROE +12.1%
Liq 2,980  12.7% away
----------------------------------------
tap: actions  swipe: next  2x: exit
```

Tap, pick "Close 50%", and tap again within 5 seconds:

```
TRIPWIRE            TESTNET      09:05
CONFIRM  Close 50%  ETH-PERP LONG
SELL 0.25 ETH  reduce-only IOC
at ~3,494.4 mark, max 5% slippage
Tap again within 5s to SEND.
----------------------------------------
tap: SEND  swipe/2x: cancel
```

> **Status: Day 2.** The HUD is live, the Telegram bot runs, and the actions menu with two-tap
> reduce-only closes is built and tested end to end in the simulator (against a fake exchange).
> It hasn't placed a real testnet order yet, because that needs a funded testnet account and an
> agent key. With no account configured, the HUD shows the live ETH mark price.

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
  Every third-party call happens here.
- **`docs/`**: [architecture](docs/architecture.md), [WebSocket protocol](docs/ws-protocol.md),
  and the [build log](docs/BUILD_LOG.md) (every decision and deviation, with where each knob lives).

## Setup

You need **Node.js 20+** (22 recommended), **[uv](https://docs.astral.sh/uv/)**, **Python 3.12**
(uv can fetch it), **ngrok** with a free account, and the **Even App** on your phone (2.2.10+)
if you want to use real glasses.

1. **Clone and install everything:**
   ```sh
   git clone https://github.com/Tejasv-Singh/tripwire.git
   cd tripwire
   npm run setup          # uv sync (backend) + npm install (plugin)
   ```
2. **Configure the backend.** Copy `.env.example` to `.env` in the repo root. Each variable has a
   one-line comment saying where to get it.
   - `PAIRING_CODE` is required. Generate one with
     `python -c "import secrets; print(secrets.token_urlsafe(12))"`.
   - `HL_ACCOUNT_ADDRESS` is optional. Leave it empty to see only the ETH price.
   - `TELEGRAM_BOT_TOKEN` is optional. Leave it empty and the bot is skipped.
   - `HL_AGENT_KEY` is optional. Without it (or without `HL_ACCOUNT_ADDRESS`) the menu still
     opens, but trading is off and nothing can be sent. See
     [Trading on testnet](#trading-on-testnet-optional).
   - `ANTHROPIC_API_KEY` is not used yet.
3. **Configure the plugin.** Copy `plugin/.env.example` to `plugin/.env.local`. Set
   `VITE_BACKEND_URL` to your HTTPS backend origin, and `VITE_PAIRING_CODE` to the same value as
   `PAIRING_CODE`. The pairing code is only read in development builds.
4. **Set up the tunnel once.** Install ngrok with
   `winget install --id Ngrok.Ngrok --exact --source winget` (or see
   [ngrok.com/download](https://ngrok.com/download)). Then **open a new terminal**, run
   `ngrok update` (winget ships 3.3.1, which predates the `--url` flag), and then
   `ngrok config add-authtoken <your token from dashboard.ngrok.com>`.
5. **If you use a different backend host**, put both `https://<host>` and `wss://<host>` in the
   `network.whitelist` of `plugin/app.json`, and update `VITE_BACKEND_URL` and the `tunnel` script
   in `package.json`.
6. **Run it.** Use one terminal per command, in this order:
   ```sh
   npm run backend:dev    # 1. backend on http://127.0.0.1:8000 (auto-reload)
   npm run tunnel         # 2. ngrok http --url=caucus-strategic-endorse.ngrok-free.dev 8000
   npm run plugin:dev     # 3. Vite on http://localhost:5173 (wait for "ready")
   npm run sim            # 4. Even Hub simulator pointed at the Vite server
   ```
   The simulated glasses show the live ETH mark price. It changes as testnet ETH moves, and the
   clock in the header keeps ticking. In the simulator, swipe for next/prev and click (tap) for
   the actions menu. Double-click exits from the HUD and backs out of any other screen.
7. **Run on real glasses:** from `plugin/`, run `npx evenhub qr --url http://<your-LAN-IP>:5173`
   and scan the code with the Even App. The phone must be on the same Wi-Fi. To build an
   installable package, run `npm run plugin:pack` to produce `plugin/tripwire.ehpk`.

**Checks:** `npm test` runs the backend pytest suite and the plugin vitest suite.
`npm run check` also runs ruff, strict mypy and tsc. `curl http://127.0.0.1:8000/healthz` shows
the feed and bot status.

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
4. The backend log shows `Trading ready on testnet: agent wallet 0x1234...abcd`, and `/healthz`
   shows `trading.status` = `ready`.
5. Open a small position on the testnet site. It shows up on the glasses within a few seconds.
   Tap, swipe to `Close 25%`, tap, check the confirm screen, then tap again within 5 s.

On the glasses:

| Screen | Tap | Swipe | Double tap |
|---|---|---|---|
| HUD | open the actions menu | next / previous position | exit the app |
| Menu | choose (Close 25 / 50 / 100 %, Back) | move the cursor | back to the HUD |
| Confirm | **send** (between 0.3 s and 5 s after choosing) | cancel, back to the menu | cancel, back to the HUD |
| Result | back to the HUD (also after 8 s) | same | same |

Every confirmed close is a reduce-only IOC order at mid ± 5 %. It is written to the SQLite
`order_log` table (`backend/data/tripwire.sqlite3`) along with Hyperliquid's answer.

### Telegram bot (optional)

1. In Telegram, open **@BotFather** (blue check), send `/newbot`, give it a name (`Tripwire`)
   and a username ending in `bot`. Copy the token. Treat it like a password; `/revoke` replaces it.
2. `/mybots` → your bot → **Bot Settings** → **Mode Settings**: turn **Secretary Mode** on. This
   is BotFather's current name for what used to be "Business Mode". Leave the other modes off.
   Turning **Allow Groups** off is recommended, since Tripwire only works in private chats.
3. Put the token in `.env` as `TELEGRAM_BOT_TOKEN=…`, then restart the backend. Auto-reload only
   watches `backend/src`, not `.env`.
4. The backend log shows `telegram=starting`, then `Telegram bot running as @your_bot`.
   `/healthz` → `telegram.status` is `running`. Send the bot `/start` (you should get an intro) and
   forward it any message (you should get `Echo: …`).
5. Connecting the bot to your other chats needs **Telegram Premium**:
   Settings → Telegram Business → Chatbots → `@your_bot` → pick the chats. The backend then logs
   `business_connection …`. Without Premium, `/start`, the echo and future alerts in the bot's own
   chat still work.

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
| Menu says `Trading off: read-only (no agent key)` | `HL_ACCOUNT_ADDRESS` and `HL_AGENT_KEY` must both be set in `.env`. Restart the backend after editing `.env`. |
| Backend won't start: `HL_AGENT_KEY must be empty or a 64-hex-char private key` | The value isn't a private key. Copy the agent's private key (64 hex characters, `0x` optional), not its address. |
| Result: `User or API Wallet ... does not exist` | The agent wallet isn't authorized for that account on **testnet**, or `HL_ACCOUNT_ADDRESS` is the agent's address instead of the main account's. |
| Result: `Order could not immediately match ...` | The IOC order found no liquidity within 5 % of mid, which is common on thin testnet books. Nothing was filled; try again. |
| Result: `No reply in 25s - CHECK positions` | Hyperliquid didn't answer in time. The order **may still have gone through**, so check the position before you try again. |
| `/healthz` shows `trading.status` = `error` | The backend couldn't load Hyperliquid's coin list at startup. `trading.detail` says why. The first close tries again. |
| `evenhub-simulator` not found | `npm run sim` calls the simulator's `bin/index.js` directly. If you run it yourself, `npm rebuild @evenrealities/evenhub-simulator` restores the link. |

## Safety rules

1. **Testnet only.** The backend refuses to start unless the Hyperliquid URL is exactly
   `hyperliquid.utils.constants.TESTNET_API_URL`. The check runs at startup and again inside the
   feed. There is no mainnet switch.
2. **Agent key only.** Orders are signed with a Hyperliquid **API/agent wallet** key
   (`HL_AGENT_KEY`), never your main wallet key. Agent wallets can't withdraw funds. Only the
   agent's public address is ever logged.
3. **Reduce-only.** The only order the glasses can send is a `reduce_only` IOC close of 25, 50
   or 100 % of a position. It can shrink or close a position but never open, grow or flip one.
   No menu item opens a position.
4. **Two-tap confirm.** The first tap shows the exact order (side, size, price, slippage). Only
   a second tap 0.3–5 s later sends it, and only if the position is still open on the same side.
   The backend enforces this, so a buggy or replayed client can't skip it.
5. **No advice.** Tripwire shows numbers you can verify (position, uPnL, liquidation distance).
   It doesn't tell you what to trade. Nothing here is financial advice.
6. **Keys stay on the backend.** The plugin never holds API keys. It only knows the backend URL
   and a pairing code, and the dev pairing code is stripped from production builds.

All six rules are enforced in code as of Day 2: `config.py` (testnet guard, key validation),
`trading.py` (reduce-only closes only), `actions.py` (two-tap confirm) and `hud.ts` (numbers
only). Tests cover each one (`backend/tests/test_actions.py`, `test_trading.py`, `test_app.py`).

## Privacy

- **Bot API only.** Tripwire uses the official Telegram Bot API through python-telegram-bot. It
  has no userbot, no MTProto client, and never logs into your Telegram account.
- **Only chats you connect.** Through Telegram Business (Settings → Business → Chatbots), you
  choose which chats the bot can see. Disconnecting removes its access.
- **No message retention.** Message text is never written to the database or the logs. The bot
  records metadata only: connection id, chat id, message id and length. The SQLite schema has no
  column for message text, and a test enforces that. Logs also redact the pairing code and the
  bot token.

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
| [pytest](https://pytest.org), [Ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org), [HTTPX](https://www.python-httpx.org) | 9.2 / 0.17 / 2.4 / 0.28 | MIT / MIT / MIT / BSD-3-Clause | tests, lint, types |
| [uv](https://docs.astral.sh/uv/) | — | MIT / Apache-2.0 | Python env and lockfile |
| [ngrok](https://ngrok.com) | — | proprietary service | HTTPS tunnel to the laptop |

## License

[MIT](LICENSE) © 2026 Tejasv Singh Hada
