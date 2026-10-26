"""data/runs.sqlite: runs, events, snapshots and the append-only ledger.

WAL mode. One Writer thread per database owns the only write connection; runs and branches hand it batches (one
per round). Readers open their own connections and read in parallel. Keys never reach this module: a run records
only provider profile ids.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from adapters.keys import redact
from simschema.episode import episode_lines, read_episode
from simschema.events import SIM_EVENT, EventBase

T = TypeVar("T")
log = logging.getLogger("sim.store")
GENESIS = "sha256:" + "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    kind TEXT NOT NULL,
    split TEXT NOT NULL,
    seed INTEGER NOT NULL,
    condition_id TEXT,
    scenario_id TEXT NOT NULL,
    scenario_hash TEXT NOT NULL,
    preset_id TEXT,
    config_hash TEXT,
    cloned_from TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    header_json TEXT NOT NULL,
    episode_hash TEXT,
    export_path TEXT,
    export_hash TEXT
);
CREATE INDEX IF NOT EXISTS runs_parent ON runs(parent_run_id);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    t REAL NOT NULL,
    json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS snapshots (
    run_id TEXT NOT NULL,
    t INTEGER NOT NULL,
    hash TEXT NOT NULL,
    blob TEXT NOT NULL,
    PRIMARY KEY (run_id, t)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS run_configs (
    run_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    parent_run_id TEXT,
    config_json TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    branch_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    line INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_run ON ledger(run_id);
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_runs (
    experiment_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    kind TEXT NOT NULL,
    idx INTEGER NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (experiment_id, condition_id, seed, kind, idx)
);
CREATE TABLE IF NOT EXISTS experiment_blocks (
    experiment_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (experiment_id, condition_id, seed)
);
CREATE TABLE IF NOT EXISTS test_cells (
    condition_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (condition_id, seed)
);
CREATE TABLE IF NOT EXISTS archived (
    kind TEXT NOT NULL,
    id TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (kind, id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS held (
    run_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS test_cells_no_update BEFORE UPDATE ON test_cells BEGIN SELECT RAISE(ABORT, 'a test cell is used once'); END;
CREATE TRIGGER IF NOT EXISTS test_cells_no_delete BEFORE DELETE ON test_cells BEGIN SELECT RAISE(ABORT, 'a test cell is used once'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chain(prev_hash: str, entry_json: str) -> str:
    return "sha256:" + hashlib.sha256((prev_hash + entry_json).encode("utf-8")).hexdigest()


PAID = "(json_extract(json, '$.cache_hit') = 0 OR json_extract(json, '$.charged') = 1 OR COALESCE(json_extract(json, '$.cost_usd'), 0) > 0)"
"""SQL: an llm_call that counted against a budget. cost_usd > 0 covers charged replays recorded before `charged` existed."""


class RunsDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.write_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

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

    @contextmanager
    def write_conn(self) -> Iterator[sqlite3.Connection]:
        """A direct write connection. Only the Writer and maintenance code use it."""
        conn = self._open()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def run(self, run_id: str) -> dict | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def events(self, run_id: str, from_seq: int = 0) -> list[EventBase]:
        with self.read() as conn:
            rows = conn.execute("SELECT json FROM events WHERE run_id = ? AND seq >= ? ORDER BY seq", (run_id, from_seq)).fetchall()
        return [SIM_EVENT.validate_json(r["json"]) for r in rows]

    def snapshots(self, run_id: str) -> dict[int, tuple[str, str]]:
        with self.read() as conn:
            rows = conn.execute("SELECT t, hash, blob FROM snapshots WHERE run_id = ? ORDER BY t", (run_id,)).fetchall()
        return {r["t"]: (r["hash"], r["blob"]) for r in rows}

    def test_cell(self, condition_id: str, seed: int) -> str | None:
        """The run id that used this test cell, or None."""
        with self.read() as conn:
            row = conn.execute("SELECT run_id FROM test_cells WHERE condition_id = ? AND seed = ?", (condition_id, seed)).fetchone()
        return row[0] if row else None

    def test_cell_count(self) -> int:
        with self.read() as conn:
            return conn.execute("SELECT COUNT(*) FROM test_cells").fetchone()[0]

    def run_config(self, run_id: str) -> dict | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM run_configs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def runs(self) -> list[dict]:
        """Every run with a stored config, newest first: config, spec, the runs row (null while queued) and counts."""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT rc.run_id, rc.batch_id, rc.parent_run_id, rc.config_json, rc.spec_json, rc.branch_json, rc.created_at,"
                " r.status, r.ended_at, r.header_json, r.episode_hash, r.export_hash,"
                " COALESCE(c.calls, 0) AS calls, COALESCE(c.cache_hits, 0) AS cache_hits, COALESCE(c.cost, 0) AS cost_usd,"
                " COALESCE(f.fallbacks, 0) AS fallbacks, COALESCE(f.clamps, 0) AS clamps, er.experiment_id,"
                " (SELECT MAX(t) FROM events e WHERE e.run_id = rc.run_id AND e.type = 'settled') AS last_t,"
                " (SELECT json_extract(json, '$.end_reason') FROM events e WHERE e.run_id = rc.run_id AND e.type = 'run_ended') "
                "AS end_reason,"
                " (SELECT json_extract(json, '$.error') FROM events e WHERE e.run_id = rc.run_id AND e.type = 'run_ended') AS error,"
                " (SELECT json_extract(json, '$.error_detail') FROM events e WHERE e.run_id = rc.run_id AND e.type = 'run_ended') "
                "AS error_detail"
                " FROM run_configs rc LEFT JOIN runs r ON r.run_id = rc.run_id"
                " LEFT JOIN experiment_runs er ON er.run_id = rc.run_id"
                " LEFT JOIN (SELECT run_id, COUNT(*) AS calls, SUM(json_extract(json, '$.cache_hit')) AS cache_hits,"
                "            SUM(COALESCE(json_extract(json, '$.cost_usd'), 0)) AS cost FROM events WHERE type = 'llm_call' "
                "GROUP BY run_id) c ON c.run_id = rc.run_id"
                " LEFT JOIN (SELECT run_id, SUM(json_extract(json, '$.fallback') = 1) AS fallbacks,"
                "            SUM(json_extract(json, '$.asked_price') IS NOT NULL) AS clamps FROM events WHERE type = 'acted' "
                "GROUP BY run_id) f"
                "   ON f.run_id = rc.run_id"
                " ORDER BY rc.created_at DESC, rc.run_id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def archived(self, kind: str) -> dict[str, str]:
        """id -> archived_at of the archived runs or experiments (DECISIONS-29). The records themselves never change."""
        with self.read() as conn:
            return dict(conn.execute("SELECT id, archived_at FROM archived WHERE kind = ?", (kind,)).fetchall())

    def held(self) -> dict[str, str]:
        """run_id -> the code of why this server cannot build the run (RunService._hold). A row is removed when the run starts."""
        with self.read() as conn:
            return dict(conn.execute("SELECT run_id, code FROM held").fetchall())

    def experiments(self) -> list[dict]:
        with self.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM experiments ORDER BY created_at DESC, id DESC").fetchall()]

    def experiment(self, experiment_id: str) -> dict | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        return dict(row) if row else None

    def experiment_runs(self, experiment_id: str) -> list[dict]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT * FROM experiment_runs WHERE experiment_id = ? ORDER BY condition_id, seed, kind, idx", (experiment_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def batch_runs(self, batch_id: str) -> list[str]:
        with self.read() as conn:
            return [r[0] for r in conn.execute("SELECT run_id FROM run_configs WHERE batch_id = ?", (batch_id,))]

    def paid_calls(self, run_ids: list[str]) -> dict:
        """Paid calls over these runs: {calls, usd, tokens, unpriced}. A cache hit counts only if it was charged (a lost
        round's reply). `unpriced`: paid calls with cost_usd null, so `usd` leaves them out."""
        if not run_ids:
            return {"calls": 0, "usd": 0.0, "tokens": 0, "unpriced": 0}
        marks = ",".join("?" * len(run_ids))
        with self.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(json_extract(json, '$.cost_usd'), 0)), 0),"
                " COALESCE(SUM(COALESCE(json_extract(json, '$.input_tokens'), 0) + COALESCE(json_extract(json, '$.output_tokens'), 0)), 0),"
                " COALESCE(SUM(json_extract(json, '$.cost_usd') IS NULL), 0) FROM events"
                f" WHERE type = 'llm_call' AND run_id IN ({marks}) AND {PAID}",
                run_ids,
            ).fetchone()
        return {"calls": int(row[0]), "usd": float(row[1]), "tokens": int(row[2]), "unpriced": int(row[3])}

    def profile_calls(self, profile_id: str, since: str) -> tuple[list[str], int]:
        """Wall times of the paid calls of one profile since `since` (ISO, UTC), over every run of this data root, and the
        number of rate_limit errors in that time."""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT json_extract(json, '$.wall_time'), json_extract(json, '$.error') FROM events"
                " WHERE type = 'llm_call' AND json_extract(json, '$.provider_profile_id') = ?"
                f" AND json_extract(json, '$.wall_time') >= ? AND {PAID}",
                (profile_id, since),
            ).fetchall()
        return [r[0] for r in rows], sum(r[1] == "rate_limit" for r in rows)

    def experiment_blocks(self, experiment_id: str) -> dict[tuple[str, int], dict]:
        """The last error of each cell that `advance` could not move on, keyed by (condition_id, seed)."""
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM experiment_blocks WHERE experiment_id = ?", (experiment_id,)).fetchall()
        return {(r["condition_id"], r["seed"]): {"code": r["code"], "message": r["message"], "at": r["at"]} for r in rows}

    def experiment_of(self, run_id: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT experiment_id FROM experiment_runs WHERE run_id = ?", (run_id,)).fetchone()
        return row[0] if row else None

    def ledger(self, run_id: str | None = None) -> list[dict]:
        """Ledger entries in line order, each with its line, prev_hash and hash. With run_id, only that run's entries."""
        sql, args = "SELECT line, entry_json, prev_hash, hash FROM ledger", ()
        if run_id is not None:
            sql, args = sql + " WHERE run_id = ?", (run_id,)
        with self.read() as conn:
            rows = conn.execute(sql + " ORDER BY line", args).fetchall()
        return [{**json.loads(r["entry_json"]), "line": r["line"], "prev_hash": r["prev_hash"], "hash": r["hash"]} for r in rows]

    def verify_ledger(self) -> tuple[bool, int | None]:
        """(True, None), or (False, first line whose hash does not follow from the line before)."""
        prev = GENESIS
        with self.read() as conn:
            for row in conn.execute("SELECT line, entry_json, prev_hash, hash FROM ledger ORDER BY line"):
                if row["prev_hash"] != prev or row["hash"] != _chain(prev, row["entry_json"]):
                    return False, row["line"]
                prev = row["hash"]
        return True, None


class Writer:
    """The single writer of one database: a thread that runs submitted jobs in order, each in one transaction.

    A job's future resolves only after its commit, so an acknowledged write survives a hard exit. After close(), submit raises.
    """

    def __init__(self, db: RunsDB) -> None:
        self.db = db
        self._jobs: queue.Queue[tuple[Callable[[sqlite3.Connection], Any], Future] | None] = queue.Queue()
        self._lock, self._closed = threading.Lock(), False
        self._thread = threading.Thread(target=self._loop, name=f"writer:{db.path.name}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        conn = self.db._open()
        try:
            while (job := self._jobs.get()) is not None:
                fn, fut = job
                try:
                    result = fn(conn)
                    conn.commit()
                except BaseException as e:  # noqa: BLE001
                    conn.rollback()
                    fut.set_exception(e)
                else:
                    fut.set_result(result)
        finally:
            conn.close()

    def submit(self, fn: Callable[[sqlite3.Connection], T]) -> Future[T]:
        fut: Future[T] = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError(f"writer for {self.db.path.name} is closed")
            self._jobs.put((fn, fut))
        return fut

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._jobs.put(None)
        self._thread.join()


class StoreSink:
    """EventSink that buffers one round and commits it through the Writer at snapshot_saved and run_ended."""

    FLUSH_ON = {"snapshot_saved", "run_ended", "run_status_changed"}

    def __init__(self, writer: Writer) -> None:
        self.writer = writer
        self._events: list[EventBase] = []
        self._snapshots: dict[tuple[str, int], tuple[str, str]] = {}

    def event(self, e: EventBase) -> None:
        self._events.append(e)
        if e.type in self.FLUSH_ON:
            self.flush()

    def flush(self) -> None:
        self.write(self._events, self._snapshots)
        self._events, self._snapshots = [], {}

    @property
    def pending(self) -> list[EventBase]:
        """The buffered events of the round in progress. A run that fails mid-round ends through this sink, so its
        round (with its llm_call events and their spend) and run_ended are one commit. A run that resumes replays it."""
        return list(self._events)

    def snapshot(self, run_id: str, t: int, snapshot_hash: str, blob: str) -> None:
        self._snapshots[(run_id, t)] = (snapshot_hash, blob)

    def write(self, events: Sequence[EventBase], snapshots: dict[tuple[str, int], tuple[str, str]]) -> None:
        """Idempotent: a batch sent twice changes nothing (INSERT OR IGNORE on (run_id, seq) and (run_id, t))."""
        events, snapshots = list(events), dict(snapshots)
        self.writer.submit(lambda conn: _write_batch(conn, events, snapshots)).result()


def _write_batch(conn: sqlite3.Connection, events: list[EventBase], snapshots: dict[tuple[str, int], tuple[str, str]]) -> None:
    for e in events:
        if e.type == "run_started":
            h = e.header
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, parent_run_id, kind, split, seed, condition_id, scenario_id, scenario_hash,"
                " preset_id, config_hash, cloned_from, status, created_at, header_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    h.run_id,
                    h.branch.parent_run_id if h.branch else None,
                    h.kind,
                    h.split,
                    h.seed,
                    h.condition_id,
                    h.scenario.id,
                    h.scenario.hash,
                    h.preset_id,
                    h.config_hash,
                    h.cloned_from,
                    "queued",
                    h.created_at,
                    h.model_dump_json(),
                ),
            )
    conn.executemany(
        "INSERT OR IGNORE INTO events (run_id, seq, type, t, json) VALUES (?,?,?,?,?)",
        [(e.run_id, e.seq, e.type, e.t, e.model_dump_json()) for e in events],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO snapshots (run_id, t, hash, blob) VALUES (?,?,?,?)",
        [(run_id, t, h, blob) for (run_id, t), (h, blob) in snapshots.items()],
    )
    for e in events:
        if e.type == "run_status_changed":
            conn.execute("UPDATE runs SET status = ? WHERE run_id = ? AND ended_at IS NULL", (e.status, e.run_id))
        elif e.type == "run_ended":
            conn.execute(
                "UPDATE runs SET status = ?, ended_at = ?, episode_hash = ? WHERE run_id = ?",
                (e.status, e.wall_time, e.episode_hash, e.run_id),
            )


def _write_trail(path: Path, events: list[EventBase], cache_path: Path) -> None:
    """Best effort (CR-66 addendum 2): a sidecar error never keeps a run from its ledger line; the export rebuilds it."""
    from adapters.trail import trail_text  # here, not at import: it loads Pydantic AI (~20 s cold), which a run without a cache never needs

    tmp = path.with_suffix(".jsonl.tmp")
    try:
        tmp.write_bytes(trail_text(events, cache_path).encode("utf-8"))
        os.replace(tmp, path)
    except Exception as e:
        log.error("trail.jsonl of %s not written: %s: %s", path.parent.name, type(e).__name__, redact(str(e)))
        tmp.unlink(missing_ok=True)


def finish_run(db: RunsDB, writer: Writer, run_id: str, runs_dir: Path, cache_path: Path | None = None) -> dict:
    """Export runs/<run_id>/episode.jsonl, then append the run's ledger line. A run is complete only after this.
    With `cache_path`, also write the sidecar runs/<run_id>/trail.jsonl from the events and the call cache (DECISIONS-14 §2.2);
    it is not in the episode, its hash or the ledger line."""
    lines = episode_lines(db.events(run_id))
    header, events = read_episode(lines)
    data = ("\n".join(lines) + "\n").encode("utf-8")
    out = Path(runs_dir) / run_id / "episode.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".jsonl.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    if cache_path is not None:
        _write_trail(out.with_name("trail.jsonl"), events, cache_path)
    export_hash = "sha256:" + hashlib.sha256(data).hexdigest()
    end = events[-1]
    entry = {
        "run_id": run_id,
        "batch_id": header.batch_id,
        "parent_run_id": header.branch.parent_run_id if header.branch else None,
        "kind": header.kind,
        "split": header.split,
        "seed": header.seed,
        "condition_id": header.condition_id,
        "labels": header.labels.model_dump() if header.labels else None,
        "parent_labels": header.parent_labels.model_dump() if header.parent_labels else None,
        "scenario_id": header.scenario.id,
        "scenario_hash": header.scenario.hash,
        "preset_id": header.preset_id,
        "config_hash": header.config_hash,
        "cloned_from": header.cloned_from,
        "models": sorted({a.model for a in header.agents if a.model}),
        "status": end.status,
        "end_reason": end.end_reason,
        "episode_hash": end.episode_hash,
        "export_hash": export_hash,
        "cost_usd": round(sum(e.cost_usd or 0 for e in events if e.type == "llm_call"), 6),
        "demand": header.demand,
        "reproducible": header.reproducible,
        "created_at": header.created_at,
        "ended_at": end.wall_time,
    }

    def append(conn: sqlite3.Connection) -> dict:
        conn.execute("UPDATE runs SET export_path = ?, export_hash = ? WHERE run_id = ?", (str(out), export_hash, run_id))
        row = conn.execute("SELECT hash FROM ledger ORDER BY line DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        entry_json = _canonical(entry)
        digest = _chain(prev, entry_json)
        cur = conn.execute("INSERT INTO ledger (run_id, entry_json, prev_hash, hash) VALUES (?,?,?,?)", (run_id, entry_json, prev, digest))
        return {**entry, "line": cur.lastrowid, "prev_hash": prev, "hash": digest}

    return writer.submit(append).result()


def put_experiment(writer: Writer, experiment_id: str, hash_: str, body_json: str, created_at: str) -> None:
    writer.submit(
        lambda conn: conn.execute(
            "INSERT INTO experiments (id, hash, body_json, created_at) VALUES (?,?,?,?)", (experiment_id, hash_, body_json, created_at)
        )
    ).result()


def put_experiment_runs(writer: Writer, experiment_id: str, rows: list[tuple[str, int, str, int, str]]) -> None:
    """rows: (condition_id, seed, kind, idx, run_id). A row that exists is kept, so the run id of a cell never changes."""
    writer.submit(
        lambda conn: conn.executemany(
            "INSERT OR IGNORE INTO experiment_runs (experiment_id, condition_id, seed, kind, idx, run_id) VALUES (?,?,?,?,?,?)",
            [(experiment_id, *r) for r in rows],
        )
    ).result()


def put_experiment_block(writer: Writer, experiment_id: str, cell: tuple[str, int], code: str, message: str, at: str) -> None:
    writer.submit(
        lambda conn: conn.execute(
            "INSERT OR REPLACE INTO experiment_blocks (experiment_id, condition_id, seed, code, message, at) VALUES (?,?,?,?,?,?)",
            (experiment_id, *cell, code, message, at),
        )
    ).result()


def clear_experiment_block(writer: Writer, experiment_id: str, cell: tuple[str, int]) -> None:
    writer.submit(
        lambda conn: conn.execute(
            "DELETE FROM experiment_blocks WHERE experiment_id = ? AND condition_id = ? AND seed = ?", (experiment_id, *cell)
        )
    ).result()


class TestCellUsed(Exception):
    """A test cell (condition, seed) already belongs to another run. Nothing of the job was stored."""

    def __init__(self, condition_id: str, seed: int, run_id: str) -> None:
        super().__init__(f"{condition_id} seed {seed} is used by {run_id}")
        self.condition_id, self.seed, self.run_id = condition_id, seed, run_id


def put_run_configs(writer: Writer, rows: list[tuple], test_cells: list[tuple[str, int, str]], created_at: str) -> None:
    """Store run configs (put_run_config's arguments without the writer) and lock their test cells in one transaction.
    A cell already locked by the same run id is a resume, not a rerun. A cell locked by another run raises TestCellUsed
    and the whole job rolls back (DECISIONS-11 §2, CR-17 U3-3, U3-5)."""

    def job(conn: sqlite3.Connection) -> None:
        for cid, seed, run_id in test_cells:
            conn.execute(
                "INSERT OR IGNORE INTO test_cells (condition_id, seed, run_id, created_at) VALUES (?,?,?,?)",
                (cid, seed, run_id, created_at),
            )
            owner = conn.execute("SELECT run_id FROM test_cells WHERE condition_id = ? AND seed = ?", (cid, seed)).fetchone()[0]
            if owner != run_id:
                raise TestCellUsed(cid, seed, owner)
        conn.executemany(
            "INSERT OR IGNORE INTO run_configs (run_id, batch_id, parent_run_id, config_json, spec_json, branch_json, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            rows,
        )

    writer.submit(job).result()


def put_run_config(
    writer: Writer,
    run_id: str,
    batch_id: str,
    parent_run_id: str | None,
    config_json: str,
    spec_json: str,
    branch_json: str | None,
    created_at: str,
) -> None:
    writer.submit(
        lambda conn: conn.execute(
            "INSERT OR IGNORE INTO run_configs (run_id, batch_id, parent_run_id, config_json, spec_json, branch_json, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, batch_id, parent_run_id, config_json, spec_json, branch_json, created_at),
        )
    ).result()
