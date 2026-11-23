"""Tiny SQLite store. Holds ONLY metadata — never message text (privacy rule, see README):

  business_connections  one row per Telegram Business connection (who connected the bot, enabled?)
  intent_log            every intent the glasses sent (audit trail for later risk actions)

Each call opens its own short-lived connection; callers run these via asyncio.to_thread().
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS business_connections (
    id            TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    user_chat_id  INTEGER NOT NULL,
    is_enabled    INTEGER NOT NULL,
    can_reply     INTEGER NOT NULL,
    connected_at  INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS intent_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    action  TEXT NOT NULL,
    client  TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def upsert_business_connection(
        self,
        *,
        connection_id: str,
        user_id: int,
        user_chat_id: int,
        is_enabled: bool,
        can_reply: bool,
        connected_at: int,
    ) -> None:
        now = int(time.time())
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO business_connections
                    (id, user_id, user_chat_id, is_enabled, can_reply, connected_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    is_enabled = excluded.is_enabled,
                    can_reply  = excluded.can_reply,
                    updated_at = excluded.updated_at
                """,
                (
                    connection_id,
                    user_id,
                    user_chat_id,
                    int(is_enabled),
                    int(can_reply),
                    connected_at,
                    now,
                ),
            )

    def business_connection_count(self) -> int:
        with closing(self._connect()) as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM business_connections").fetchone()
            return int(n)

    def log_intent(self, action: str, client: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO intent_log (ts, action, client) VALUES (?, ?, ?)",
                (int(time.time() * 1000), action, client),
            )

    def recent_intents(self, limit: int = 20) -> list[tuple[int, str, str]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT ts, action, client FROM intent_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]
