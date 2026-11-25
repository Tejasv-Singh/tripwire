"""Demo backend for the simulator (and hackathon demos) -- no funded account needed.

    npm run backend:demo                         # FAKE exchange: a fake 0.5 ETH long with a stop,
                                                 #   on LIVE testnet ETH prices
    npm run backend:demo -- --watch 0xabc...     # WATCH a real testnet address (read-only)
    ... --no-telegram                            # record DMs / journal posts, never send them
    ... --no-journal                             # as if JOURNAL_CHAT_ID were unset (dry-run print)

Runs on http://127.0.0.1:8010 (point a Vite dev server at it with VITE_BACKEND_URL). It never
starts Telegram long polling (that would fight the real backend over the same bot token), but it
can still SEND the owner a DM if TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID are set; otherwise
"DMs" are recorded and printed.

Demo-only routes (localhost only, not in the real app):
  POST /demo/forward    {"text": "BTC long 96.5k sl 95k tp 99k"}  -> the real Telegram draft
                        handler, as if the owner had forwarded it; returns the bot's reply
  POST /demo/threshold  {"pct": 20}                                -> same as /threshold 20
  POST /demo/business   {"name": "@alice", "text": "still in?"}   -> a business message
  POST /demo/voice      {"wav_path": "C:/.../close_half.wav"}      -> speech for the next
                        push-to-talk (16 kHz mono WAV), for machines without a virtual mic
  GET  /demo/dms                                                   -> DMs sent / recorded
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import wave
from types import SimpleNamespace
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request

from tripwire.config import Settings
from tripwire.exchanges.fake import FakeAdapter
from tripwire.main import create_app
from tripwire.state import MarketState
from tripwire.telegram_bot import make_draft_handler

log = logging.getLogger("tripwire.demo")
DEMO_OWNER_ID = 1  # stands in for TELEGRAM_OWNER_ID when the real one is not set

FAKE_ETH = {
    "coin": "ETH",
    "szi": "0.5",
    "entryPx": "1650.0",
    "liquidationPx": "1420.0",
    "leverage": {"type": "cross", "value": 5},
    "positionValue": "840.0",
}


class DemoNotifier:
    """Sends a real DM when a bot token and owner id exist; always records what was sent."""

    def __init__(self, token: str, owner_id: int | None) -> None:
        self.token, self.owner_id = token, owner_id
        self.sent: list[dict[str, Any]] = []

    async def send_owner(self, text: str) -> bool:
        delivered = False
        if self.token and self.owner_id:
            from telegram import Bot

            try:
                async with Bot(self.token) as bot:  # sendMessage only: no polling, no conflict
                    await bot.send_message(chat_id=self.owner_id, text=text)
                delivered = True
            except Exception as exc:
                log.warning("demo DM failed: %s: %s", type(exc).__name__, exc)
        self.sent.append({"text": text, "delivered_to_telegram": delivered})
        log.warning("DM %s:\n%s", "SENT" if delivered else "RECORDED (no owner id)", text)
        return True  # counts as delivered for the demo either way


def build(args: argparse.Namespace) -> FastAPI:
    real = Settings()  # type: ignore[call-arg]  # pairing code, bot token, owner id from .env
    overrides: dict[str, Any] = {
        "cors_origins": f"http://localhost:{args.vite_port},http://127.0.0.1:{args.vite_port}",
        "db_path": "data/demo.sqlite3",
        "telegram_owner_id": real.telegram_owner_id or DEMO_OWNER_ID,
        "alert_liq_pct": args.alert_pct,
    }
    if args.watch:
        overrides.update(watch_address=args.watch, hl_account_address="")
    if args.no_journal:  # as if JOURNAL_CHAT_ID were unset: the summary is a dry-run print
        overrides["journal_chat_id"] = None
    settings = Settings(**{**real.model_dump(), **overrides})  # validated (model_copy is not)
    make_adapter = None
    if not args.watch:

        def make_adapter(state: MarketState) -> FakeAdapter:
            return FakeAdapter(state, [FAKE_ETH], fill_delay_s=1.0, stops={"ETH": 1500.0})

    next_speech: dict[str, bytes | None] = {"pcm": None}

    def audio_override() -> bytes | None:
        """POST /demo/voice queues synthesized speech for the NEXT push-to-talk; it replaces
        whatever the simulator's microphone recorded, before transcription."""
        pcm, next_speech["pcm"] = next_speech["pcm"], None
        return pcm

    app = create_app(
        settings, run_bot=False, make_adapter=make_adapter, audio_override=audio_override
    )
    # --no-telegram: record DMs / journal posts only. (In PowerShell, $env:TELEGRAM_BOT_TOKEN=''
    # DELETES the variable, so .env's token wins: that is how one real DM slipped out on Day 4.)
    token = "" if args.no_telegram else real.telegram_bot_token.get_secret_value()
    notifier = DemoNotifier(token, real.telegram_owner_id)
    app.state.alerts.notifier = notifier
    services = app.state.bot_services
    if args.no_telegram:
        log.warning("--no-telegram: DMs and journal posts are only recorded, never sent")
    journal: list[dict[str, Any]] = []

    async def send_to_chat(chat_id: int, text: str) -> tuple[bool, str]:
        """Journal post without polling: one sendMessage with the real bot token."""
        if not token:
            journal.append({"chat_id": chat_id, "text": text, "delivered": False})
            return False, "no TELEGRAM_BOT_TOKEN"
        from telegram import Bot

        try:
            async with Bot(token) as bot:
                await bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:
            journal.append({"chat_id": chat_id, "text": text, "delivered": False})
            return False, f"{type(exc).__name__}: {exc}"[:80]
        journal.append({"chat_id": chat_id, "text": text, "delivered": True})
        log.warning("JOURNAL posted to %s", chat_id)
        return True, "posted to the journal"

    app.state.bot.send_to_chat = send_to_chat

    @app.get("/demo/journal")
    async def journal_posts(request: Request) -> list[dict[str, Any]]:
        local_only(request)
        return journal

    def local_only(request: Request) -> None:
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "demo routes are localhost-only")

    @app.post("/demo/forward")
    async def forward(request: Request, text: str = Body(..., embed=True)) -> dict[str, Any]:
        local_only(request)
        replies: list[str] = []

        async def reply_text(t: str) -> None:
            replies.append(t)

        message = SimpleNamespace(
            text=text, caption=None, chat_id=services.owner_id, message_id=0,
            forward_origin=object(), reply_text=reply_text,
        )  # fmt: skip
        update = SimpleNamespace(
            message=message, effective_user=SimpleNamespace(id=services.owner_id)
        )
        await make_draft_handler(services).callback(update, None)  # type: ignore[arg-type]
        return {"bot_reply": replies}

    @app.post("/demo/voice")
    async def voice(request: Request, wav_path: str = Body(..., embed=True)) -> dict[str, Any]:
        """Use this WAV (16 kHz, 16-bit, mono) as the speech of the next push-to-talk. For
        machines without a virtual microphone; the long-press / mic / streaming path still runs."""
        local_only(request)
        with wave.open(wav_path) as w:
            if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (16000, 2, 1):
                raise HTTPException(400, "need a 16 kHz, 16-bit, mono WAV")
            next_speech["pcm"] = w.readframes(w.getnframes())
        return {"queued_seconds": round(len(next_speech["pcm"] or b"") / 32000, 2)}

    @app.post("/demo/threshold")
    async def threshold(request: Request, pct: float = Body(..., embed=True)) -> dict[str, Any]:
        local_only(request)
        await asyncio.to_thread(app.state.alerts.set_threshold, pct)
        return {"threshold_pct": pct}

    @app.post("/demo/business")
    async def business(
        request: Request, name: str = Body(...), text: str = Body(...)
    ) -> dict[str, Any]:
        local_only(request)
        app.state.inbox.add(connection_id="demo", chat_id=0, name=name, text=text)
        return {"inbox": app.state.inbox.summary()}

    @app.get("/demo/dms")
    async def dms(request: Request) -> list[dict[str, Any]]:
        local_only(request)
        return notifier.sent

    return app


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--watch", default="", help="WATCH this testnet address instead of the fake exchange"
    )
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--vite-port", type=int, default=5174, help="Vite dev server port (CORS)")
    ap.add_argument(
        "--alert-pct", type=float, default=5.0, help="initial liquidation alert threshold"
    )
    ap.add_argument(
        "--no-telegram",
        action="store_true",
        help="never send to Telegram: Away-mode DMs and journal posts are only recorded "
        "(GET /demo/dms, /demo/journal)",
    )
    ap.add_argument(
        "--no-journal",
        action="store_true",
        help="ignore JOURNAL_CHAT_ID: posting the session summary is a dry-run print to the log",
    )
    args = ap.parse_args()
    uvicorn.run(build(args), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
