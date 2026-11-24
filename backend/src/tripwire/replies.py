"""Short reply drafts (<= 60 characters) to the last business chat, offered after a fill.

Source: OpenRouter's OpenAI-compatible chat API (OPENROUTER_API_KEY, OPENROUTER_MODEL; default a
":free" model). If the key is missing, the call fails, times out, is rate-limited (429), or the
answer is empty / longer than 60 characters, a template is used instead
("Trimmed half of my ETH.", "Moved my ETH stop to 1,700.").

Privacy: OpenRouter only ever receives the trade facts below (action, coin, side, size fraction,
price, new stop). Never the chat's messages, names or ids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAX_REPLY_CHARS = 60
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-31b-it:free"  # a ":free" model that existed on 2026-11-24
TIMEOUT_S = 8.0
SYSTEM_PROMPT = (
    "You write one very short, casual Telegram reply (at most 60 characters) from a trader to a "
    "friend who just messaged them. Use only the facts given. No advice, no predictions, no "
    "emojis, no hashtags, no quotes. Reply with the message text only."
)


@dataclass(frozen=True, slots=True)
class ReplyContext:
    kind: str  # "close" | "stop" | "open"
    coin: str
    side: str  # position side
    fraction: float | None = None  # close: 0.25 / 0.5 / 1.0
    price: float | None = None  # fill price, if known
    sl: float | None = None  # new stop-loss, if the action set one

    def facts(self) -> str:
        side = self.side.lower()
        if self.kind == "close":
            share = {1.0: "all", 0.5: "half", 0.25: "a quarter"}.get(self.fraction or 0, "part")
            text = f"I just closed {share} of my {self.coin} {side}"
            if self.price:
                text += f" at about {self.price:,.6g}"
        elif self.kind == "stop":
            text = f"I just moved the stop-loss on my {self.coin} {side} to {self.sl:,.6g}"
        else:
            text = f"I just opened a small {self.coin} {side}"
        return text + "."


def template_reply(ctx: ReplyContext) -> str:
    if ctx.kind == "close":
        if ctx.fraction is not None and ctx.fraction >= 1:
            text = f"Closed my {ctx.coin} {ctx.side.lower()}."
        else:
            share = {0.5: "half", 0.25: "a quarter"}.get(ctx.fraction or 0, "some")
            text = f"Trimmed {share} of my {ctx.coin}."
        if ctx.sl is not None:
            text = text[:-1] + f", SL moved to {ctx.sl:,.6g}."
    elif ctx.kind == "stop" and ctx.sl is not None:
        text = f"Moved my {ctx.coin} stop to {ctx.sl:,.6g}."
    else:
        text = f"Opened a small {ctx.coin} {ctx.side.lower()}."
    return text[:MAX_REPLY_CHARS]


def clean_reply(raw: Any) -> str | None:
    """First line, no wrapping quotes, single spaces; None if empty or over 60 characters."""
    if not isinstance(raw, str):
        return None
    line = next((x for x in raw.strip().splitlines() if x.strip()), "")
    text = " ".join(line.split()).strip().strip("\"'`").strip()
    if not text or len(text) > MAX_REPLY_CHARS:
        return None
    return text


class ReplyDrafter:
    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        *,
        timeout_s: float = TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key.strip()
        self.model = model.strip() or DEFAULT_MODEL
        self.timeout_s = timeout_s
        self._transport = transport
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def draft(self, ctx: ReplyContext) -> tuple[str, str]:
        """(text, source) with source "openrouter" or "template"."""
        if self._key:
            try:
                text = await self._openrouter(ctx)
            except Exception as exc:  # 429, timeout, network, bad JSON: fall back, never fail
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.info("OpenRouter reply draft failed, using a template (%s)", self.last_error)
                text = None
            if text:
                return text, "openrouter"
        return template_reply(ctx), "template"

    async def _openrouter(self, ctx: ReplyContext) -> str | None:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Facts: {ctx.facts()} Write the reply."},
            ],
            "max_tokens": 40,
            "temperature": 0.4,
        }
        headers = {"Authorization": f"Bearer {self._key}", "X-Title": "Tripwire"}
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as client:
            resp = await client.post(OPENROUTER_URL, json=body, headers=headers)
            resp.raise_for_status()  # 429 (free-tier rate limit) -> template
            data = resp.json()
        text = clean_reply(data["choices"][0]["message"]["content"])
        if text is None:
            self.last_error = "empty or longer than 60 characters"
        return text
