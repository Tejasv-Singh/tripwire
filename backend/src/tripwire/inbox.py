"""In-memory inbox of Telegram Business messages, for the HUD line "TG 1 new @name: <30 chars>".

Privacy (README "No message retention"): only the first PREVIEW_CHARS characters of a message are
kept, only in this process's memory, only for WINDOW_S; nothing here is logged or written to
SQLite. The reply target (which business chat to answer after a fill) keeps ids and the sender's
display name, never text. Messages the owner sends themselves are not counted.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PREVIEW_CHARS = 30
WINDOW_S = 600.0  # a message counts as "new" on the HUD for 10 minutes
REPLY_WINDOW_S = 1800.0  # offer a reply to a chat that wrote in the last 30 minutes


@dataclass(frozen=True, slots=True)
class InboxItem:
    at: float
    name: str
    preview: str


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    connection_id: str  # business_connection_id: lets the bot answer as the user
    chat_id: int
    name: str
    at: float


class Inbox:
    def __init__(
        self,
        *,
        window_s: float = WINDOW_S,
        on_change: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_s = window_s
        self.on_change = on_change
        self._clock = clock
        self._items: deque[InboxItem] = deque(maxlen=50)
        self._target: ReplyTarget | None = None

    def add(self, *, connection_id: str, chat_id: int, name: str, text: str) -> None:
        now = self._clock()
        preview = " ".join(text.split())[:PREVIEW_CHARS]  # the rest of the text is dropped here
        self._items.append(InboxItem(now, name, preview))
        self._target = ReplyTarget(connection_id, chat_id, name, now)
        if self.on_change is not None:
            self.on_change()

    def _prune(self, now: float) -> None:
        while self._items and now - self._items[0].at > self.window_s:
            self._items.popleft()

    def summary(self) -> dict[str, Any] | None:
        """{"count", "from", "preview"} for the HUD, or None when nothing is new."""
        self._prune(self._clock())
        if not self._items:
            return None
        latest = self._items[-1]
        return {"count": len(self._items), "from": latest.name, "preview": latest.preview}

    def reply_target(self, max_age_s: float = REPLY_WINDOW_S) -> ReplyTarget | None:
        t = self._target
        if t is None or self._clock() - t.at > max_age_s:
            return None
        return t

    def clear(self) -> None:
        self._items.clear()
        if self.on_change is not None:
            self.on_change()
