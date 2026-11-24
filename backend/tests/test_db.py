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


def test_order_log_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path / "o.sqlite3")
    store.init()
    store.log_order(
        client="1.2.3.4:5", coin="ETH", side="LONG", fraction=0.5, size=0.25, ok=True,
        status="filled", filled_size=0.25, avg_px=3499.1, oid=77, message="Filled 0.25 @ 3499.1",
    )  # fmt: skip
    store.log_order(
        client="1.2.3.4:5", coin="ETH", side="LONG", fraction=1.0, size=None, ok=False,
        status="rejected", filled_size=None, avg_px=None, oid=None, message="nope",
    )  # fmt: skip
    rows = store.recent_orders()
    assert [r["status"] for r in rows] == ["rejected", "filled"]  # newest first
    assert rows[0]["size"] is None and rows[1]["avg_px"] == 3499.1 and rows[1]["ok"] == 1
