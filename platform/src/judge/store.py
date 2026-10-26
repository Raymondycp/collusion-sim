"""data/eval.sqlite: judge jobs, their planned calls, the append-only call records and the test judgement locks
(DECISIONS-9 §9, §14; plan §2.5). Apart from runs.sqlite, so the judge never writes to an episode.

Keys never reach this module: a call record holds a profile id, never a key or a header."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapters.store import Writer

GENESIS = "sha256:" + "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY,
    body_json TEXT NOT NULL,
    item_set_hash TEXT NOT NULL,
    order_seed INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_json TEXT
);
CREATE TABLE IF NOT EXISTS judge_plan (
    evaluation_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    split TEXT NOT NULL,
    view TEXT NOT NULL,
    transform TEXT NOT NULL,
    backend TEXT NOT NULL,
    paraphrase INTEGER NOT NULL,
    block TEXT NOT NULL,
    PRIMARY KEY (evaluation_id, idx)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS judge_calls (
    line INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id TEXT NOT NULL,
    plan_idx INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS judge_calls_eval ON judge_calls(evaluation_id);
CREATE TABLE IF NOT EXISTS judge_cells (
    run_id TEXT NOT NULL,
    view TEXT NOT NULL,
    transform TEXT NOT NULL,
    backend TEXT NOT NULL,
    paraphrase INTEGER NOT NULL,
    evaluation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, view, transform, backend, paraphrase)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS judge_calls_no_update BEFORE UPDATE ON judge_calls
    BEGIN SELECT RAISE(ABORT, 'judge calls are append-only'); END;
CREATE TRIGGER IF NOT EXISTS judge_calls_no_delete BEFORE DELETE ON judge_calls
    BEGIN SELECT RAISE(ABORT, 'judge calls are append-only'); END;
CREATE TRIGGER IF NOT EXISTS judge_cells_no_update BEFORE UPDATE ON judge_cells
    BEGIN SELECT RAISE(ABORT, 'a test judgement is made once'); END;
CREATE TRIGGER IF NOT EXISTS judge_cells_no_delete BEFORE DELETE ON judge_cells
    BEGIN SELECT RAISE(ABORT, 'a test judgement is made once'); END;
"""

CHARGED = "json_extract(entry_json, '$.charged') = 1"
"""SQL: a call that counts in the judge ledger (a real request; not a cache hit, not no_talk, not the scripted judge)."""
RETRY = "json_extract(entry_json, '$.retry_of') IS NOT NULL"
"""SQL: a retry. It is paid from the retry reserve only, never from a block (CR-33 m3)."""
HOLDING = (
    "(e.status IN ('queued', 'running') OR (e.status = 'failed' AND EXISTS (SELECT 1 FROM judge_cells k WHERE k.evaluation_id = e.id)))"
)
"""SQL: a job whose planned calls without a record hold ledger room. A failed job holds room only while it owns test cells,
since only those must resume under its id (CR-33 b, n1); a failed job without test cells meets the room check again at
resume."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chain(prev_hash: str, entry_json: str) -> str:
    return "sha256:" + hashlib.sha256((prev_hash + entry_json).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlannedCall:
    idx: int
    run_id: str
    split: str
    view: str
    transform: str
    backend: str
    paraphrase: int
    block: str

    @property
    def cell(self) -> tuple[str, str, str, str, int]:
        return (self.run_id, self.view, self.transform, self.backend, self.paraphrase)


class BudgetExceeded(Exception):
    """The ledger has no room for the job, read inside the transaction that would store it (CR-33 m2)."""


class JudgementUsed(Exception):
    """A test judgement (run, view, transform, backend, paraphrase) already belongs to another evaluation."""

    def __init__(self, cell: tuple, evaluation_id: str) -> None:
        super().__init__(f"{cell} is judged by {evaluation_id}")
        self.cell, self.evaluation_id = cell, evaluation_id


class EvalDB:
    """The same connection rules as RunsDB (WAL, one Writer thread)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._open()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    def evaluation(self, evaluation_id: str) -> dict | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)).fetchone()
        return dict(row) if row else None

    def evaluations(self) -> list[dict]:
        with self.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM evaluations ORDER BY created_at DESC, id DESC")]

    def plan(self, evaluation_id: str) -> list[PlannedCall]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT idx, run_id, split, view, transform, backend, paraphrase, block FROM judge_plan "
                "WHERE evaluation_id = ? ORDER BY idx",
                (evaluation_id,),
            ).fetchall()
        return [PlannedCall(**dict(r)) for r in rows]

    def calls(self, evaluation_id: str) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT line, plan_idx, entry_json, hash FROM judge_calls WHERE evaluation_id = ? ORDER BY line", (evaluation_id,)
            ).fetchall()
        return [{**json.loads(r["entry_json"]), "line": r["line"], "plan_idx": r["plan_idx"], "hash": r["hash"]} for r in rows]

    def ledger_state(self) -> LedgerState:
        with self.read() as conn:
            return ledger_state(conn)

    def open_plans(self) -> list[tuple[str, PlannedCall]]:
        with self.read() as conn:
            return _open_plans(conn)

    def judgement(self, cell: tuple) -> str | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT evaluation_id FROM judge_cells WHERE run_id = ? AND view = ? AND transform = ? AND backend = ? AND paraphrase = ?",
                cell,
            ).fetchone()
        return row[0] if row else None

    def verify(self) -> tuple[bool, int | None]:
        prev = GENESIS
        with self.read() as conn:
            for row in conn.execute("SELECT line, entry_json, prev_hash, hash FROM judge_calls ORDER BY line"):
                if row["prev_hash"] != prev or row["hash"] != _chain(prev, row["entry_json"]):
                    return False, row["line"]
                prev = row["hash"]
        return True, None


@dataclass(frozen=True)
class LedgerState:
    """first: charged first calls by (backend, block); retries: charged retries by backend; held: planned calls of
    holding jobs with no record yet."""

    first: dict[tuple[str, str], int]
    retries: dict[str, int]
    held: list[PlannedCall]


def _open_plans(conn: sqlite3.Connection, exclude: str | None = None) -> list[tuple[str, PlannedCall]]:
    rows = conn.execute(
        "SELECT p.evaluation_id, p.idx, p.run_id, p.split, p.view, p.transform, p.backend, p.paraphrase, p.block "
        "FROM judge_plan p JOIN evaluations e ON e.id = p.evaluation_id "
        f"WHERE {HOLDING} AND e.id IS NOT ? AND NOT EXISTS "
        "(SELECT 1 FROM judge_calls c WHERE c.evaluation_id = p.evaluation_id AND c.plan_idx = p.idx)",
        (exclude,),
    ).fetchall()
    return [(r["evaluation_id"], PlannedCall(**{k: r[k] for k in r.keys() if k != "evaluation_id"})) for r in rows]


def ledger_state(conn: sqlite3.Connection, exclude: str | None = None) -> LedgerState:
    """exclude: a job whose own held calls are left out (its resume checks them as new)."""
    rows = conn.execute(
        "SELECT json_extract(entry_json, '$.backend') AS b, json_extract(entry_json, '$.block') AS k, "
        f"{RETRY} AS retry, COUNT(*) AS n FROM judge_calls WHERE {CHARGED} GROUP BY b, k, retry"
    ).fetchall()
    first: dict[tuple[str, str], int] = {}
    retries: dict[str, int] = {}
    for r in rows:
        if r["retry"]:
            retries[r["b"]] = retries.get(r["b"], 0) + r["n"]
        else:
            first[(r["b"], r["k"])] = r["n"]
    return LedgerState(first, retries, [p for _, p in _open_plans(conn, exclude)])


def put_evaluation(
    writer: Writer,
    evaluation_id: str,
    body: dict,
    item_set_hash: str,
    order_seed: int,
    plan: list[PlannedCall],
    created_at: str,
    room: Callable[[LedgerState], str | None],
) -> None:
    """Store the job, its plan and the locks of its test calls in one transaction. `room` reads the ledger inside it and
    names the cap the job would pass, which raises BudgetExceeded (CR-33 m2). A lock held by another evaluation raises
    JudgementUsed. Either way nothing is stored (DECISIONS-9 §7 "judged once"; CR-21 F7)."""

    def job(conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if problem := room(ledger_state(conn)):
            raise BudgetExceeded(problem)
        for p in plan:
            if p.split != "test":
                continue
            conn.execute(
                "INSERT OR IGNORE INTO judge_cells (run_id, view, transform, backend, paraphrase, evaluation_id, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (*p.cell, evaluation_id, created_at),
            )
            owner = conn.execute(
                "SELECT evaluation_id FROM judge_cells WHERE run_id = ? AND view = ? AND transform = ? AND backend = ? AND paraphrase = ?",
                p.cell,
            ).fetchone()[0]
            if owner != evaluation_id:
                raise JudgementUsed(p.cell, owner)
        conn.execute(
            "INSERT INTO evaluations (id, body_json, item_set_hash, order_seed, status, created_at) VALUES (?,?,?,?,?,?)",
            (evaluation_id, _canonical(body), item_set_hash, order_seed, "queued", created_at),
        )
        conn.executemany(
            "INSERT INTO judge_plan (evaluation_id, idx, run_id, split, view, transform, backend, paraphrase, block) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(evaluation_id, p.idx, p.run_id, p.split, p.view, p.transform, p.backend, p.paraphrase, p.block) for p in plan],
        )

    writer.submit(job).result()


def append_call(writer: Writer, evaluation_id: str, plan_idx: int, entry: dict) -> dict:
    def job(conn: sqlite3.Connection) -> dict:
        row = conn.execute("SELECT hash FROM judge_calls ORDER BY line DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        entry_json = _canonical(entry)
        digest = _chain(prev, entry_json)
        cur = conn.execute(
            "INSERT INTO judge_calls (evaluation_id, plan_idx, entry_json, prev_hash, hash) VALUES (?,?,?,?,?)",
            (evaluation_id, plan_idx, entry_json, prev, digest),
        )
        return {**entry, "line": cur.lastrowid, "plan_idx": plan_idx, "hash": digest}

    return writer.submit(job).result()


def reopen(writer: Writer, evaluation_id: str, room: Callable[[LedgerState], str | None]) -> bool:
    """A failed job back to queued under the same id; its plan and test locks stay (CR-33 b). `room` reads the ledger
    without the job's own held calls, inside this transaction, and a cap it names raises BudgetExceeded (CR-33 n1). False
    if the job was not failed."""

    def job(conn: sqlite3.Connection) -> bool:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if problem := room(ledger_state(conn, evaluation_id)):
            raise BudgetExceeded(problem)
        cur = conn.execute(
            "UPDATE evaluations SET status = 'queued', finished_at = NULL, error_json = NULL WHERE id = ? AND status = 'failed'",
            (evaluation_id,),
        )
        return cur.rowcount == 1

    return writer.submit(job).result()


def set_status(writer: Writer, evaluation_id: str, status: str, at: str, error: dict | None = None) -> None:
    """queued -> running (started_at) -> completed | stopped | failed (finished_at)."""
    column = "started_at" if status == "running" else "finished_at"

    def job(conn: sqlite3.Connection) -> None:
        conn.execute(
            f"UPDATE evaluations SET status = ?, {column} = COALESCE({column}, ?), error_json = ? WHERE id = ?",
            (status, at, _canonical(error) if error else None, evaluation_id),
        )

    writer.submit(job).result()
