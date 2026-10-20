"""Durable alert storage for the self-hosted version (SQLite, standard library only).

Alerts survive restarts, and the same idempotency rule as the AWS version
applies: the transaction id is the primary key, so a redelivered record
can never create a second alert. Swap in PostgreSQL by keeping this
interface (save / recent / get / stats) and changing the SQL connection.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  txn_id     TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL,
  ts         REAL NOT NULL,
  decision   TEXT NOT NULL,
  risk_score REAL NOT NULL,
  country    TEXT,
  payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (ts DESC);
CREATE INDEX IF NOT EXISTS alerts_user ON alerts (user_id, ts DESC);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


class AlertStore:
    def __init__(self, path: str | Path = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def save(self, alert: dict) -> bool:
        """Insert an alert. Returns False if this transaction was already stored."""
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?,?)",
                (alert["txn_id"], alert["user_id"], alert["ts"], alert["decision"], alert["risk_score"],
                 alert.get("country"), json.dumps(alert, separators=(",", ":"))))
            return cur.rowcount == 1

    def recent(self, limit: int = 100, user_id: str | None = None) -> list[dict]:
        q, args = "SELECT payload FROM alerts", []
        if user_id:
            q, args = q + " WHERE user_id = ?", [user_id]
        with self.lock:
            rows = self.db.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def get(self, txn_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT payload FROM alerts WHERE txn_id = ?", (txn_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def bump(self, **deltas: int) -> None:
        with self.lock, self.db:
            for name, d in deltas.items():
                self.db.execute("INSERT INTO counters VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = value + ?",
                                (name, d, d))

    def stats(self) -> dict:
        with self.lock:
            rows = self.db.execute("SELECT name, value FROM counters").fetchall()
        s = {"processed": 0, "flagged": 0, "blocked": 0}
        s.update(dict(rows))
        return s
