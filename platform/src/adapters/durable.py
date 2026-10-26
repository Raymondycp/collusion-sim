"""Durable runs with DBOS on SQLite (data/dbos.sqlite): one workflow per run, workflow id = run id.

Crash recovery only. After a crash, DBOS starts the workflow again; it resumes at the run's last committed round
(snapshot + stored events), so seq and the episode hash continue. Model calls are not DBOS steps: a resumed run
skips rounds, which would shift DBOS's step order. The call cache, keyed by request content, makes resent calls free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dbos import DBOS, SetWorkflowID, WorkflowHandle

from adapters.store import RunsDB, StoreSink, Writer, finish_run
from simcore.agents import AgentPolicy
from simcore.recorder import Recorder, utc_now
from simcore.run import HostPolicy, Run, RunControl, RunSpec
from simcore.scenario import Scenario
from simcore.state import RunState
from simschema.events import BranchInfo


@dataclass
class RunParts:
    """Everything needed to build a run again in a new process. `policies` gets the run's recorder."""

    spec: RunSpec
    scenario: Scenario
    policies: Callable[[Recorder], dict[str, AgentPolicy]]
    host: HostPolicy | None = None
    branch: BranchInfo | None = None
    state: RunState | None = None
    rounds: range | None = None
    control: RunControl | None = None


def resume_or_start(
    db: RunsDB,
    writer: Writer,
    parts: RunParts,
    runs_dir: Path,
    now: Callable[[], str] = utc_now,
    sink: StoreSink | None = None,
    cache_path: Path | None = None,
) -> dict:
    """Idempotent: a finished run returns its ledger entry; a stored partial run resumes; a new run starts.
    `sink`: the caller's StoreSink, so it can commit the buffered round of a run that fails."""
    run_id = parts.spec.run_id
    done = db.ledger(run_id)
    if done:
        return done[-1]
    prior = db.events(run_id)
    if prior and prior[-1].type == "run_ended":
        return finish_run(db, writer, run_id, runs_dir, cache_path)
    rounds = parts.rounds or range(1, parts.scenario.horizon.stop_round(parts.spec.seed) + 1)
    state = parts.state
    if prior:
        snapshots = db.snapshots(run_id)
        last = max((t for t in snapshots if t >= rounds.start - 1), default=None)
        if last is not None:
            state = RunState.restore(snapshots[last][1])
            rounds = range(last + 1, rounds.stop)
    rec = Recorder(run_id, [sink or StoreSink(writer)], now=now, prior=prior)
    run = Run(
        parts.spec,
        parts.scenario,
        parts.policies(rec),
        rec,
        host=parts.host,
        state=state,
        branch=parts.branch,
        rounds=rounds,
        control=parts.control,
    )
    run.execute(resume=bool(prior))
    return finish_run(db, writer, run_id, runs_dir, cache_path)


_ctx: dict = {}
MAX_PARALLEL_RUNS = 4


@DBOS.workflow(name="sim_run")
def _sim_run(run_id: str) -> dict:
    return _ctx["execute"](run_id)


def launch(dbos_path: Path, execute: Callable[[str], dict]) -> None:
    """Start DBOS. It recovers every run workflow that was pending when the last process died.

    `execute(run_id)` runs or resumes one run and records a failure as a failed run, so a run error never leaves an
    ERROR workflow (DBOS does not recover those) with a run that stays "running"."""
    _ctx.update(execute=execute)
    Path(dbos_path).parent.mkdir(parents=True, exist_ok=True)
    DBOS(config={"name": "collusion-sim", "system_database_url": "sqlite:///" + Path(dbos_path).resolve().as_posix()})
    DBOS.launch()
    DBOS.register_queue("runs", worker_concurrency=MAX_PARALLEL_RUNS, polling_interval_sec=0.2)


def start(run_id: str) -> WorkflowHandle[dict]:
    with SetWorkflowID(run_id):
        return DBOS.enqueue_workflow("runs", _sim_run, run_id)


def retrieve(run_id: str) -> WorkflowHandle[dict]:
    return DBOS.retrieve_workflow(run_id)


def shutdown() -> None:
    DBOS.destroy(workflow_completion_timeout_sec=5)
