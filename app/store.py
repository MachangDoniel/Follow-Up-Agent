"""SQLite persistence: cooldown state plus an audit trail of every decision.

Everything the agent decides is written here, sent or not, so you can see after
the fact exactly what it did and why.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS followups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT    NOT NULL,
    contact_id   TEXT    NOT NULL,
    contact_name TEXT    NOT NULL DEFAULT '',
    reason       TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT 'call',
    sent         INTEGER NOT NULL DEFAULT 0,
    skip_reason  TEXT    NOT NULL DEFAULT '',
    text         TEXT    NOT NULL DEFAULT '',
    created_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followups_contact
    ON followups (platform, contact_id, kind, sent, created_at);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(followups)")}
        if "kind" not in existing:
            self._db.execute(
                "ALTER TABLE followups ADD COLUMN kind TEXT NOT NULL DEFAULT 'call'"
            )

    def close(self) -> None:
        self._db.close()

    def seconds_since_last_send(
        self, platform: str, contact_id: str, kind: str = "call"
    ) -> float | None:
        """How long since we last actually sent this contact something, or None."""
        row = self._db.execute(
            "SELECT created_at FROM followups "
            "WHERE platform = ? AND contact_id = ? AND kind = ? AND sent = 1 "
            "ORDER BY created_at DESC LIMIT 1",
            (platform, contact_id, kind),
        ).fetchone()
        return None if row is None else time.time() - row[0]

    def record(
        self,
        *,
        platform: str,
        contact_id: str,
        contact_name: str,
        reason: str,
        sent: bool,
        skip_reason: str,
        text: str,
        kind: str = "call",
    ) -> None:
        self._db.execute(
            "INSERT INTO followups "
            "(platform, contact_id, contact_name, reason, kind, sent, skip_reason, text, "
            " created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                platform,
                contact_id,
                contact_name,
                reason,
                kind,
                int(sent),
                skip_reason,
                text,
                time.time(),
            ),
        )
        self._db.commit()

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        self._db.row_factory = sqlite3.Row
        try:
            return self._db.execute(
                "SELECT * FROM followups ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            self._db.row_factory = None
