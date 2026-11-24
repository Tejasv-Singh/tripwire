"""Tiny SQLite store. Holds ONLY metadata — never message text (privacy rule, see README):

  business_connections  one row per Telegram Business connection (who connected the bot, enabled?)
  intent_log            every intent the glasses sent (audit trail for later risk actions)
  order_log             every reduce-only close that was confirmed on the glasses, and its result
  kv                    small persistent switches (kill switch, alert threshold), no secrets
  sessions              one row per ended trading session (the summary as JSON; numbers only)

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
CREATE TABLE IF NOT EXISTS order_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,          -- ms since epoch, when the result came back
    client       TEXT NOT NULL,
    coin         TEXT NOT NULL,
    side         TEXT NOT NULL,             -- side of the position that was reduced
    fraction     REAL NOT NULL,             -- 0.25 / 0.5 / 1.0
    size         REAL,                      -- coin units requested; NULL = whole position
    ok           INTEGER NOT NULL,
    status       TEXT NOT NULL,             -- filled / resting / rejected / error
    filled_size  REAL,
    avg_px       REAL,
    oid          INTEGER,
    message      TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'close'   -- close / stop / open
);
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ms    INTEGER NOT NULL,
    ended_ms      INTEGER NOT NULL,
    summary_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
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
            # migration: Day-2 databases have order_log without `kind`
            cols = {row[1] for row in conn.execute("PRAGMA table_info(order_log)")}
            if "kind" not in cols:
                conn.execute("ALTER TABLE order_log ADD COLUMN kind TEXT NOT NULL DEFAULT 'close'")

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

    def log_order(
        self,
        *,
        client: str,
        coin: str,
        side: str,
        fraction: float,
        size: float | None,
        ok: bool,
        kind: str = "close",
        status: str,
        filled_size: float | None,
        avg_px: float | None,
        oid: int | None,
        message: str,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO order_log (ts, client, coin, side, fraction, size, ok, status,
                                       filled_size, avg_px, oid, message, kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time() * 1000),
                    client,
                    coin,
                    side,
                    fraction,
                    size,
                    int(ok),
                    status,
                    filled_size,
                    avg_px,
                    oid,
                    message,
                    kind,
                ),
            )

    def recent_orders(self, limit: int = 20) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM order_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def log_session(self, *, started_ms: int, ended_ms: int, summary_json: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO sessions (started_ms, ended_ms, summary_json) VALUES (?, ?, ?)",
                (started_ms, ended_ms, summary_json),
            )

    def recent_sessions(self, limit: int = 10) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def kv_get(self, key: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return None if row is None else str(row[0])

    def kv_set(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, int(time.time())),
            )

    def recent_intents(self, limit: int = 20) -> list[tuple[int, str, str]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT ts, action, client FROM intent_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]
