"""SQLite store: metadata only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tripwire.db import Store


def test_store_upserts_connections_and_logs_intents(tmp_path: Path) -> None:
    store = Store(tmp_path / "nested" / "t.sqlite3")
    store.init()
    store.init()  # idempotent
    kw = dict(connection_id="bc1", user_id=1, user_chat_id=2, can_reply=True, connected_at=100)
    store.upsert_business_connection(is_enabled=True, **kw)  # type: ignore[arg-type]
    store.upsert_business_connection(is_enabled=False, **kw)  # type: ignore[arg-type]
    assert store.business_connection_count() == 1
    store.log_intent("next", "127.0.0.1:1")
    store.log_intent("prev", "127.0.0.1:1")
    assert [a for _, a, _ in store.recent_intents()] == ["prev", "next"]


def test_schema_has_no_message_text_column(tmp_path: Path) -> None:
    store = Store(tmp_path / "t.sqlite3")
    store.init()
    with sqlite3.connect(store.path) as conn:
        cols = {
            row[1]
            for table in ("business_connections", "intent_log")
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
    assert not cols & {"text", "message", "body", "content", "caption"}
