import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.durable import RunParts, resume_or_start
from adapters.store import RunsDB, Writer
from simcore.agents import PolicyContext
from simcore.recorder import MemorySink, Recorder, _hash_line
from simcore.run import Run
from simcore.scripted import GrimTrigger, make_scripted
from simkit import JOINT, NASH, capability, frozen_scenario, spec

CLOCK = "2026-10-21T00:00:00.000Z"
POLICIES = {"A": ("grim_trigger", {"target": JOINT, "punish_rounds": 3}), "B": ("price_schedule", {"prices": {10: NASH}, "default": JOINT})}


class Crash(BaseException):
    pass


class CrashingGrim(GrimTrigger):
    hard = False

    def act(self, obs, tools):
        if obs.t == 12:
            if self.hard:
                os._exit(3)
            raise Crash
        return super().act(obs, tools)


def parts(crash=False):
    scenario = frozen_scenario()
    sp = spec({s: capability() for s in POLICIES}, {s: k for s, (k, _) in POLICIES.items()})
    market = scenario.market.build()

    def policies(_recorder):
        out = {}
        for s, (k, p) in POLICIES.items():
            ctx = PolicyContext(seller_id=s, sellers=("A", "B"), market=market, params=scenario.params)
            out[s] = CrashingGrim(ctx, **p) if crash and s == "A" else make_scripted(k, ctx, **p)
        return out

    return RunParts(spec=sp, scenario=scenario, policies=policies)


def reference_events():
    p = parts()
    sink = MemorySink()
    rec = Recorder(p.spec.run_id, [sink], now=lambda: CLOCK)
    Run(p.spec, p.scenario, p.policies(rec), rec).execute()
    return sink.events


@pytest.fixture
def store(tmp_path):
    db = RunsDB(tmp_path / "runs.sqlite")
    writer = Writer(db)
    yield db, writer, tmp_path / "runs"
    writer.close()


def test_a_crashed_run_resumes_at_its_last_committed_round_and_matches_an_unbroken_run(store):
    db, writer, runs = store
    with pytest.raises(Crash):
        resume_or_start(db, writer, parts(crash=True), runs, now=lambda: CLOCK)
    stored = db.events("run_TEST")
    assert stored[-1].type == "snapshot_saved" and stored[-1].t == 11
    entry = resume_or_start(db, writer, parts(), runs, now=lambda: CLOCK)
    events = db.events("run_TEST")
    ref = reference_events()
    resumed = [e for e in events if not (e.type == "run_status_changed" and e.reason == "recovered")]
    assert [e.model_dump(exclude={"seq", "episode_hash"}) for e in resumed] == [e.model_dump(exclude={"seq", "episode_hash"}) for e in ref]
    assert [e.seq for e in events] == list(range(len(events)))
    rolled = hashlib.sha256("".join(_hash_line(e) for e in events[:-1]).encode("utf-8")).hexdigest()  # null LATER_FIELDS left out
    assert events[-1].episode_hash == "sha256:" + rolled
    assert entry["status"] == "completed" and db.verify_ledger() == (True, None)


def test_resuming_a_finished_run_changes_nothing(store):
    db, writer, runs = store
    first = resume_or_start(db, writer, parts(), runs, now=lambda: CLOCK)
    n = len(db.events("run_TEST"))
    again = resume_or_start(db, writer, parts(), runs, now=lambda: CLOCK)
    assert again == first and len(db.events("run_TEST")) == n and len(db.ledger()) == 1


def test_dbos_recovers_a_run_after_the_process_dies(tmp_path):
    child = Path(__file__).with_name("durable_child.py")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")])}
    crash = subprocess.run([sys.executable, str(child), str(tmp_path), "crash"], env=env, capture_output=True, text=True, timeout=120)
    assert crash.returncode == 3, crash.stderr[-2000:]
    db = RunsDB(tmp_path / "runs.sqlite")
    assert db.events("run_TEST")[-1].t == 11
    recover = subprocess.run([sys.executable, str(child), str(tmp_path), "recover"], env=env, capture_output=True, text=True, timeout=120)
    assert recover.returncode == 0, recover.stderr[-2000:]
    assert "status=completed" in recover.stdout
    events = db.events("run_TEST")
    assert events[-1].type == "run_ended" and [e.seq for e in events] == list(range(len(events)))
    assert (tmp_path / "runs" / "run_TEST" / "episode.jsonl").exists()
