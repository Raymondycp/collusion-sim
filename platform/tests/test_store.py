import hashlib
import sqlite3
import threading

import pytest

from adapters.store import RunsDB, StoreSink, Writer, finish_run
from simcore.branch import BranchRequest, plan_branches
from simcore.recorder import MemorySink
from simkit import JOINT, NASH, run
from simschema.episode import read_episode
from simschema.events import ProbeIntervention


@pytest.fixture
def db(tmp_path):
    database = RunsDB(tmp_path / "runs.sqlite")
    writer = Writer(database)
    yield database, writer
    writer.close()


POLICIES = {"A": ("grim_trigger", {"target": JOINT}), "B": ("constant", {"price": JOINT})}


def stored_run(db, run_id="run_TEST", policies=POLICIES):
    database, writer = db
    memory = MemorySink()
    run(policies, run_id=run_id, sinks=[memory, StoreSink(writer)])
    return memory


def test_the_database_runs_in_wal_mode(db):
    with db[0].read() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_every_event_and_snapshot_is_stored_once_in_order(db):
    memory = stored_run(db)
    database, writer = db
    assert database.events("run_TEST") == memory.events
    assert database.snapshots("run_TEST") == memory.snapshots
    assert database.run("run_TEST")["status"] == "completed"
    StoreSink(writer).write(memory.events[:5], {})
    assert database.events("run_TEST") == memory.events


def test_events_from_seq_serve_a_websocket_resume(db):
    memory = stored_run(db)
    assert db[0].events("run_TEST", from_seq=40) == memory.events[40:]


def test_finish_exports_a_parseable_episode_and_appends_a_ledger_line(db, tmp_path):
    memory = stored_run(db)
    database, writer = db
    entry = finish_run(database, writer, "run_TEST", tmp_path / "runs")
    path = tmp_path / "runs" / "run_TEST" / "episode.jsonl"
    header, events = read_episode(path.read_text(encoding="utf-8").splitlines())
    assert events == memory.events and header.run_id == "run_TEST"
    assert entry["export_hash"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry["episode_hash"] == memory.events[-1].episode_hash
    assert database.run("run_TEST")["export_hash"] == entry["export_hash"]
    assert [e["run_id"] for e in database.ledger()] == ["run_TEST"]
    assert database.verify_ledger() == (True, None)


def test_a_run_without_run_ended_cannot_be_exported(db, tmp_path):
    memory = stored_run(db)
    database, writer = db
    with database.write_conn() as conn:
        conn.execute("DELETE FROM events WHERE run_id = ? AND seq = ?", ("run_TEST", memory.events[-1].seq))
    with pytest.raises(ValueError):
        finish_run(database, writer, "run_TEST", tmp_path / "runs")


@pytest.mark.parametrize("sql", ["UPDATE ledger SET run_id = 'x'", "DELETE FROM ledger"])
def test_the_ledger_refuses_update_and_delete(db, tmp_path, sql):
    stored_run(db)
    finish_run(*db, "run_TEST", tmp_path / "runs")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db[0].write_conn() as conn:
            conn.execute(sql)
    assert len(db[0].ledger()) == 1


def test_ledger_verify_finds_an_edited_line(db, tmp_path):
    for i in range(3):
        stored_run(db, run_id=f"run_{i}")
        finish_run(*db, f"run_{i}", tmp_path / "runs")
    with db[0].write_conn() as conn:
        conn.execute("DROP TRIGGER ledger_no_update")
        conn.execute("UPDATE ledger SET entry_json = replace(entry_json, 'run_1', 'run_X') WHERE line = 2")
    assert db[0].verify_ledger() == (False, 2)


def test_a_branch_forks_from_stored_snapshots(db):
    database, writer = db
    _, _, spec, scenario, _ = run(POLICIES, sinks=[StoreSink(writer)])
    (child,) = plan_branches(
        spec,
        scenario,
        database.snapshots("run_TEST"),
        BranchRequest(at=10, rounds=3, intervention=ProbeIntervention(target="B")),
        ["run_CHILD"],
        created_at="2026-10-21T01:00:00Z",
    )
    assert child.state.t == 9 and child.state.history["A"][-1].price == JOINT


def test_parallel_runs_share_one_writer_without_lock_errors(db):
    errors = []

    def go(i):
        try:
            stored_run(db, run_id=f"run_P{i}", policies={"A": ("best_response", {}), "B": ("constant", {"price": NASH + 0.05 * i})})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    assert all(db[0].run(f"run_P{i}")["status"] == "completed" for i in range(6))
    assert all(len(db[0].events(f"run_P{i}")) == len(db[0].events("run_P0")) for i in range(6))


class SlowCommit(sqlite3.Connection):
    fail = False

    def commit(self):
        import time

        time.sleep(0.3)
        if SlowCommit.fail:
            SlowCommit.fail = False
            raise sqlite3.OperationalError("disk I/O error")
        super().commit()


@pytest.fixture
def slow_db(tmp_path, monkeypatch):
    database = RunsDB(tmp_path / "runs.sqlite")
    opened = database._open

    def slow_open():
        conn = sqlite3.connect(database.path, timeout=10, check_same_thread=False, factory=SlowCommit)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(database, "_open", slow_open)
    writer = Writer(database)
    yield database, writer, opened
    writer.close()


def count(opened):
    conn = opened()
    try:
        return conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    finally:
        conn.close()


def put(conn):
    conn.execute("INSERT INTO ledger (run_id, entry_json, prev_hash, hash) VALUES ('run_X', '{}', 'a', 'b')")


def test_an_acknowledged_write_is_visible_on_a_new_connection_even_when_the_commit_is_slow(slow_db):
    database, writer, opened = slow_db
    writer.submit(put).result(timeout=5)
    assert count(opened) == 1


def test_a_failed_commit_rejects_that_job_and_the_writer_keeps_working(slow_db):
    database, writer, opened = slow_db
    SlowCommit.fail = True
    with pytest.raises(sqlite3.OperationalError):
        writer.submit(put).result(timeout=5)
    assert count(opened) == 0
    writer.submit(put).result(timeout=5)
    assert count(opened) == 1


def test_a_closed_writer_refuses_new_jobs(db):
    database, writer = db
    writer.close()
    with pytest.raises(RuntimeError, match="closed"):
        writer.submit(put)


def test_an_ablation_child_leaves_the_condition_and_the_ledger_api_returns_its_parent_labels(db, tmp_path):
    from jsonschema import Draft202012Validator

    from services.read import ledger_entry
    from simcore.agents import PolicyContext
    from simcore.recorder import Recorder
    from simcore.scripted import make_scripted
    from simkit import capability
    from simschema.events import AblationIntervention
    from test_api import SPEC

    database, writer = db
    caps = {s: capability(channels=("private_A_B",)) for s in "AB"}
    _, _, spec, scenario, _ = run(POLICIES, caps=caps, research=True, private_pairs=True, sinks=[StoreSink(writer)])
    finish_run(database, writer, "run_TEST", tmp_path / "runs")
    ablate = BranchRequest(at=10, rounds=3, intervention=AblationIntervention(channel_id="private_A_B", mode="clear"))
    (child,) = plan_branches(spec, scenario, database.snapshots("run_TEST"), ablate, ["run_CHILD"], created_at="2026-10-21T01:00:00Z")
    market = scenario.market.build()
    built = {
        s: make_scripted(k, PolicyContext(seller_id=s, sellers=("A", "B"), market=market, params=scenario.params), **p)
        for s, (k, p) in POLICIES.items()
    }
    child.run(scenario, built, Recorder("run_CHILD", [StoreSink(writer)])).execute()
    finish_run(database, writer, "run_CHILD", tmp_path / "runs")

    entries = {e["run_id"]: ledger_entry(e) for e in database.ledger()}
    validator = Draft202012Validator({"$ref": "#/components/schemas/LedgerEntry", "components": SPEC["components"]})
    for entry in entries.values():
        validator.validate(entry)
    assert (entries["run_TEST"]["condition_id"], entries["run_TEST"]["parent_labels"]) == ("C1", None)
    assert (entries["run_CHILD"]["condition_id"], entries["run_CHILD"]["parent_labels"]) == (None, spec.labels.model_dump())
