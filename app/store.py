"""SQLite storage for received events and the delivery of each one to each destination."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    received_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(id),
    destination TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending, sent or dead
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    last_status_code INTEGER,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (event_id, destination)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (status, next_attempt_at);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def add_event(self, event_id: str, source: str, payload: dict, deliveries: dict[str, dict], now: float) -> bool:
        """Store the event and one pending delivery per destination, together or not at all.
        Returns False, storing nothing, if this event id has been received before."""
        with closing(self._connect()) as db:
            try:
                with db:
                    db.execute("INSERT INTO events (id, source, received_at, payload) VALUES (?, ?, ?, ?)",
                               (event_id, source, now, json.dumps(payload)))
                    for destination, body in deliveries.items():
                        db.execute("INSERT INTO deliveries (event_id, destination, next_attempt_at, payload, updated_at) VALUES (?, ?, ?, ?, ?)",
                                   (event_id, destination, now, json.dumps(body), now))
            except sqlite3.IntegrityError:
                return False
        return True

    def due(self, now: float, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM deliveries WHERE status='pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?", (now, limit)).fetchall()
        return [self._delivery(r) for r in rows]

    def mark_sent(self, delivery_id: int, attempts: int, status_code: int, now: float) -> None:
        self._update("UPDATE deliveries SET status='sent', attempts=?, last_status_code=?, last_error=NULL, updated_at=? WHERE id=?",
                     (attempts, status_code, now, delivery_id))

    def mark_retry(self, delivery_id: int, attempts: int, next_at: float, error: str, status_code: int | None, now: float) -> None:
        self._update("UPDATE deliveries SET attempts=?, next_attempt_at=?, last_error=?, last_status_code=?, updated_at=? WHERE id=?",
                     (attempts, next_at, error, status_code, now, delivery_id))

    def mark_dead(self, delivery_id: int, attempts: int, error: str, status_code: int | None, now: float) -> None:
        self._update("UPDATE deliveries SET status='dead', attempts=?, last_error=?, last_status_code=?, updated_at=? WHERE id=?",
                     (attempts, error, status_code, now, delivery_id))

    def replay(self, delivery_id: int, now: float) -> bool:
        """Put a dead delivery back in the queue with a fresh set of attempts."""
        with closing(self._connect()) as db, db:
            cur = db.execute("UPDATE deliveries SET status='pending', attempts=0, next_attempt_at=?, updated_at=? WHERE id=? AND status='dead'",
                             (now, now, delivery_id))
            return cur.rowcount == 1

    def _update(self, sql: str, args: tuple) -> None:
        with closing(self._connect()) as db, db:
            db.execute(sql, args)

    @staticmethod
    def _delivery(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def delivery(self, delivery_id: int) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        return self._delivery(row) if row else None

    def event(self, event_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not row:
                return None
            ds = db.execute("SELECT * FROM deliveries WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
        return {**dict(row), "payload": json.loads(row["payload"]), "deliveries": [self._delivery(d) for d in ds]}

    def events(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM events ORDER BY received_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()]
        return [self.event(i) for i in ids]
