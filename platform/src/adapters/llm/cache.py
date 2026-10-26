"""cache/calls.sqlite: request key -> reply. The source of reproducibility: a rerun with the same cache sends nothing.

failed_calls keeps the request of an attempt that got no reply (provider error, timeout), for the run's trail.jsonl
(DECISIONS-14 §2.2). It is never read as a reply: a failed key is sent again.

timeouts records a slow attempt (policy.SLOW: the client deadline or a stalled stream), keyed like a reply (DECISIONS-22
entry 5.5). It is read as an outcome, never as a reply: a rerun of the same request key gets the same failure with no
provider call, so a recorded hold or PASS replays. A fresh try of a cell needs another sample_index (SPEC)."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from adapters.llm.endpoint import Reply

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    key TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    reply_json TEXT NOT NULL,
    model TEXT NOT NULL,
    host TEXT NOT NULL,
    cost_usd REAL,
    paid_by_run TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS failed_calls (
    key TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    model TEXT NOT NULL,
    host TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS timeouts (
    key TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    model TEXT NOT NULL,
    host TEXT NOT NULL,
    provider_code TEXT,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class Cached:
    reply: Reply
    cost_usd: float | None
    paid_by_run: str | None


class CallCache:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            if "paid_by_run" not in {r[1] for r in self._conn.execute("PRAGMA table_info(calls)")}:
                self._conn.execute("ALTER TABLE calls ADD COLUMN paid_by_run TEXT")

    def get(self, key: str) -> Cached | None:
        with self._lock:
            row = self._conn.execute("SELECT reply_json, cost_usd, paid_by_run FROM calls WHERE key = ?", (key,)).fetchone()
        return Cached(Reply.from_json(row[0]), row[1], row[2]) if row else None

    def put(self, key: str, request_json: str, reply: Reply, model: str, host: str, cost_usd: float | None, run_id: str) -> None:
        """run_id: the run that paid for this reply."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO calls (key, request_json, reply_json, model, host, cost_usd, paid_by_run) VALUES (?,?,?,?,?,?,?)",
                (key, request_json, reply.to_json(), model, host, cost_usd, run_id),
            )

    def put_failed(self, key: str, request_json: str, model: str, host: str, run_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO failed_calls (key, request_json, model, host, run_id) VALUES (?,?,?,?,?)",
                (key, request_json, model, host, run_id),
            )

    def timed_out(self, key: str) -> str | None:
        """The provider code of a recorded timeout of this request key ("" when none was given), or None."""
        with self._lock:
            row = self._conn.execute("SELECT provider_code FROM timeouts WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0] or ""

    def put_timeout(self, key: str, request_json: str, model: str, host: str, provider_code: str | None, run_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO timeouts (key, request_json, model, host, provider_code, run_id) VALUES (?,?,?,?,?,?)",
                (key, request_json, model, host, provider_code, run_id),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
