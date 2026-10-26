"""Start, control and branch runs. A run's config and spec are stored before it starts, so `builder(run_id)` can
rebuild it in any process (DBOS recovery uses the same builder)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from adapters import durable
from adapters.durable import RunParts, resume_or_start
from adapters.keys import mask_secrets, redact
from adapters.llm import EndpointError
from adapters.llm.budget import BudgetGuard
from adapters.store import RunsDB, StoreSink, TestCellUsed, Writer, finish_run, put_run_config, put_run_configs
from services import estimate
from services.catalog import Catalog
from services.configs import ApiError, BudgetSettings, RunConfig, build_specs, check_scripted, make_policies
from services.llm_runs import LlmRuns, uses_llm
from services.read import write_run_folder
from services.research import Research
from simcore.branch import BranchError, BranchRequest, plan_branches
from simcore.recorder import Recorder, utc_now
from simcore.run import RunInterrupted, RunSpec
from simcore.scenario import Scenario
from simcore.state import RunState
from simschema.events import BranchInfo, ErrorDetail, RunEndedEvent, RunStatusChangedEvent

log = logging.getLogger("sim.runner")
SHUTDOWN_WAIT_S = 120
DONE = ("SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED")  # DBOS workflow states that never change again

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id(prefix: str) -> str:
    """ULID-style: 48-bit ms time + 80 random bits, Crockford base32. Sorts by creation time."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return prefix + "".join(_B32[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def error_detail(e: Exception, code: str) -> ErrorDetail:
    """run_ended.error_detail of a failed run: the code, the class, where (from simcore.run.seller_call) and the masked text.
    The endpoint's provider message is already masked with the key (it keeps model ids), so it is only cut (CR-102)."""
    http = e if isinstance(e, EndpointError) else None
    return ErrorDetail(
        code=code,
        exception=type(e).__name__,
        where=getattr(e, "sim_where", None),
        message=mask_secrets(str(e), (), 200) or None,
        http_status=http.http_status if http else None,
        provider_code=http.provider_code[:48] if http and http.provider_code else None,
        provider_message=http.message[:200] if http and http.message else None,
    )


class Control:
    """RunControl for one run, set by the API (pause, resume, stop). In memory: one local process owns the runs."""

    def __init__(self) -> None:
        self._state = "go"
        self._cv = threading.Condition()

    def check(self, t: int) -> str:
        with self._cv:
            return self._state

    def wait(self) -> str:
        with self._cv:
            while self._state == "pause":
                self._cv.wait()
            return self._state

    def set(self, state: str) -> None:
        with self._cv:
            self._state = state
            self._cv.notify_all()


class RunService:
    def __init__(
        self,
        db: RunsDB,
        writer: Writer,
        catalog: Catalog,
        runs_dir: Path,
        *,
        use_dbos: bool = False,
        max_parallel: int = 4,
        now=utc_now,
        llm: LlmRuns | None = None,
        research: Research | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self.db, self.writer, self.catalog, self.runs_dir, self.llm = db, writer, catalog, Path(runs_dir), llm
        self.cache_path = cache_path  # the call cache, for each finished run's trail.jsonl
        self.research = research
        self.use_dbos, self.now = use_dbos, now
        self._pool = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="run")
        self._controls: dict[str, Control] = {}
        self._futures: dict[str, Future] = {}
        self._active: set[str] = set()
        self._active_cv = threading.Condition()
        self._closing = False
        self.batch_guard: Callable[[str], BudgetGuard | None] = lambda run_id: None
        """The experiment's shared guard for a run, or None (set by services.experiments)."""
        self._batches: dict[str, BudgetGuard] = {}
        self._batches_lock = threading.Lock()
        self.on_finished: Callable[[str], None] = lambda run_id: None
        """Called after a run has ended and its ledger line is written. Must not block."""

    # Start

    def start(self, config: RunConfig) -> tuple[str, list[str]]:
        prepared = self.prepare(config)
        self.commit(prepared)
        return prepared[0][1].batch_id, [spec.run_id for _, spec in prepared]

    def prepare(self, config: RunConfig, run_ids: list[str] | None = None) -> list[tuple[RunConfig, RunSpec]]:
        """Check and build every run of a request; store nothing. A research config becomes one config per seed (its
        scenario, condition and prompt variant). `run_ids` (one per seed, in seed order) fixes the ids, so a caller can
        record them first and build again after a crash."""
        if run_ids is not None and len(run_ids) != len(config.seeds):
            raise ValueError("one run id per seed")
        ids = iter(run_ids or [new_id("run_") for _ in config.seeds])
        units = self._units(config)
        if self.llm:
            self.llm.check_allowed(config)
        units = [(self.check_agents(cfg, scenario), scenario, binding) for cfg, scenario, binding in units]
        if config.split == "test":
            self.check_test_cells([(b.condition_id, cfg.seeds[0]) for cfg, _, b in units], run_ids)
        if self.llm:
            self.llm.check_budget(config)
        batch_id, created = new_id("batch_"), self.now()
        stored = []
        for cfg, scenario, binding in units:
            unit_ids = [next(ids) for _ in cfg.seeds]
            specs = build_specs(cfg, scenario, batch_id, unit_ids, created, bind=self.llm.bind if self.llm else None, research=binding)
            stored += [(cfg, spec) for spec in specs]
        return stored

    def check_test_cells(self, cells: list[tuple[str, int]], run_ids: list[str] | None = None) -> None:
        """A clear refusal before anything is stored, also for an estimate. commit holds the lock itself (CR-17 U3-3);
        a cell used by one of run_ids is a resume of that run (U3-5)."""
        for cid, seed in cells:
            owner = self.db.test_cell(cid, seed)
            if owner is not None and owner not in (run_ids or []):
                raise _cell_used(cid, seed, owner)

    def check_agents(self, cfg: RunConfig, scenario: Scenario) -> RunConfig:
        """The agent checks of a start without the paid-call gate (CR-24): every scripted seller builds with its params;
        every LLM agent's profile exists, has a key and a built protocol. Returns the config with prompts written out."""
        check_scripted(cfg, scenario)
        if not self.llm:
            if uses_llm(cfg):
                raise ApiError(422, "profile_key_missing", "This server has no provider profiles configured for LLM agents.")
            return cfg
        cfg = self.llm.resolve(cfg)
        for a in cfg.agents:
            if a.policy.kind == "llm":
                self.llm.bind(a)
        return cfg

    def _units(self, config: RunConfig) -> list[tuple[RunConfig, Scenario, Any]]:
        """(config, scenario, research binding) per unit: one per seed for research, one for all seeds for exploratory."""
        if config.kind == "research":
            if self.research is None:
                raise ApiError(409, "conflict", "This server has no conditions directory, so it cannot start research runs.")
            return [(r.config, r.scenario, r.binding) for r in self.research.expand(config)]
        if self.research is not None:
            self.research.check_scenario_open(config.scenario_id)
        return [(config, self.catalog.scenario(config.scenario_id), None)]

    def estimate(self, config: RunConfig) -> dict:
        """POST /runs/estimate: the checks of a start except the paid-call gate and the budget rule (a warning here), then
        the worst case per run."""
        runs = []
        units = self._units(config)
        if config.split == "test":
            self.check_test_cells([(b.condition_id, cfg.seeds[0]) for cfg, _, b in units])
        for cfg, scenario, _ in units:
            cfg = self.check_agents(cfg, scenario)
            runs += [(cfg.model_copy(update={"seeds": [s]}), scenario.horizon.stop_round(s)) for s in cfg.seeds]
        price_of = self.llm.profiles.price_of if self.llm else (lambda _p, _m: None)
        window_of = self.llm.window_status if self.llm else (lambda _p: None)
        return estimate.runs_estimate(runs, price_of, window_of)

    def commit(self, prepared: list[tuple[RunConfig, RunSpec]]) -> None:
        """Store and launch prepared runs. A run whose config is already stored is not stored again. Test runs pass the
        freeze gate here too (an experiment's later parents start through commit) and lock their cell in the same
        transaction as their config: two parallel starts of one cell store one run."""
        tests = [(spec.condition_id, spec.seed, spec.run_id) for _, spec in prepared if spec.split == "test"]
        if tests:
            self.research.check_test_open()
        if any(spec.split == "dev" for _, spec in prepared):  # CR-29 m3: a planned dev parent after the conditions stage broke
            self.research.check_dev_open()
        for cfg, spec in prepared:  # CR-47 m3: a profile edited while an experiment runs
            if spec.split in ("dev", "test"):
                self.research.check_models_frozen(cfg.condition_id, cfg.agents)
        rows = [
            (spec.run_id, spec.batch_id, None, cfg.model_dump_json(exclude_none=True), spec.model_dump_json(), None, spec.created_at)
            for cfg, spec in prepared
        ]
        try:
            put_run_configs(self.writer, rows, tests, self.now())
        except TestCellUsed as e:
            raise _cell_used(e.condition_id, e.seed, e.run_id) from None
        for _, spec in prepared:
            self._launch(spec.run_id)

    def branch(
        self, parent_id: str, request: BranchRequest, budget: BudgetSettings | None = None, ids: list[str] | None = None
    ) -> tuple[str, list[str]]:
        """`budget` replaces the parent's budget for each child; None keeps it. `ids` fixes the child run ids."""
        row = self.db.run_config(parent_id)
        if row is None or self.db.run(parent_id) is None:
            raise ApiError(404, "not_found", f"Run {parent_id} not found.")
        config = RunConfig.model_validate_json(row["config_json"])
        if any(a.policy.kind == "imported" for a in config.agents):
            raise ApiError(409, "conflict", f"Run {parent_id} is an imported log: it has no market state to branch from.")
        parent = RunSpec.model_validate_json(row["spec_json"])
        if self.research is not None:
            if parent.split == "test":
                self.research.check_test_open()
            elif parent.split == "dev":
                self.research.check_dev_open()
            if parent.split in ("dev", "test"):  # CR-47 m3: the children start from the parent's stored config
                self.research.check_models_frozen(config.condition_id, config.agents)
            self.research.check_scenario_open(config.scenario_id)
        config = config.model_copy(update={"batch_budget": None})  # a branch batch has no shared cap of its own
        if budget is not None:
            config = config.model_copy(update={"budget": budget})
        if self.llm:
            self.llm.check_allowed(config)
            self.llm.check_budget(config, self.batch_guard(ids[0]) if ids else None)  # an experiment's children share its guard
        config_json = config.model_dump_json(exclude_none=True)
        scenario = self.catalog.scenario(config.scenario_id)
        if config.scenario_hash and scenario.hash != config.scenario_hash:
            raise ApiError(
                409, "hash_mismatch", f"Scenario {config.scenario_id} changed after run {parent_id} started; its branches cannot start."
            )
        batch_id = new_id("batch_")
        ids = ids or [new_id("run_") for _ in range(request.n)]
        try:
            children = plan_branches(parent, scenario, self.db.snapshots(parent_id), request, ids, self.now())
        except BranchError as e:
            errors = [{"path": e.path, "message": str(e)}] if e.path else None
            raise (
                ApiError(409, "snapshot_missing", str(e), errors) if e.snapshot else ApiError(422, "validation_error", str(e), errors)
            ) from None
        fresh = {c.spec.run_id for c in children if self.db.run_config(c.spec.run_id) is None}
        for c in children:
            spec = c.spec.model_copy(update={"batch_id": batch_id})
            branch = json.dumps({"branch": c.branch.model_dump(mode="json"), "rounds": request.rounds})
            put_run_config(self.writer, spec.run_id, batch_id, parent_id, config_json, spec.model_dump_json(), branch, spec.created_at)
        for c in children:
            if c.spec.run_id in fresh:  # a child stored before a crash is resumed by recovery, not launched twice
                self._launch(c.spec.run_id)
        return batch_id, ids

    def _batch(self, config: RunConfig, batch_id: str | None) -> BudgetGuard | None:
        """The guard of a POST /runs batch_budget, shared by the batch's runs; after a restart it starts from their stored calls."""
        bb = config.batch_budget
        if bb is None or batch_id is None:
            return None
        with self._batches_lock:
            if batch_id not in self._batches:
                g = BudgetGuard(max_usd=bb.max_usd, max_calls=bb.max_calls, max_tokens=bb.max_tokens)
                spent = self.db.paid_calls(self.db.batch_runs(batch_id))
                g.calls, g.spent_usd, g.tokens, g.unpriced = spent["calls"], spent["usd"], spent["tokens"], spent["unpriced"]
                self._batches[batch_id] = g
            return self._batches[batch_id]

    def builder(self, run_id: str) -> RunParts:
        row = self.db.run_config(run_id)
        if row is None:
            raise KeyError(run_id)
        config = RunConfig.model_validate_json(row["config_json"])
        spec = RunSpec.model_validate_json(row["spec_json"])
        scenario = self.catalog.scenario(config.scenario_id)
        branch = BranchInfo.model_validate(json.loads(row["branch_json"])["branch"]) if row["branch_json"] else None
        llm = None
        if self.llm and uses_llm(config):
            self.llm.check_allowed(config)
            batch = self.batch_guard(run_id) or self._batch(config, spec.batch_id)
            budget = self.llm.budget(config, self.db.events(run_id), batch=batch)
            sample_index = config.replicate * 1_000_000 + (branch.sample_index if branch else 0)
            llm = self.llm.factory(config, spec, budget, sample_index, scenario)
        parts = RunParts(
            spec=spec,
            scenario=scenario,
            policies=lambda rec: make_policies(config, scenario, spec, rec, llm),
            control=self._controls.setdefault(run_id, Control()),
        )
        if row["branch_json"]:
            b = json.loads(row["branch_json"])
            parts.branch = BranchInfo.model_validate(b["branch"])
            fork = parts.branch.fork_t
            snaps = self.db.snapshots(parts.branch.parent_run_id)
            parts.state = RunState.restore(snaps[fork - 1][1]) if fork > 1 else RunState()
            parts.rounds = range(fork, fork + b["rounds"])
        return parts

    def launch(self, run_id: str) -> None:
        """Launch a stored run. Only for a run that is not live in this process (recovery after a restart)."""
        self._launch(run_id)

    def _launch(self, run_id: str) -> None:
        self._controls.setdefault(run_id, Control())
        if self.use_dbos:
            durable.start(run_id)
        else:
            self._futures[run_id] = self._pool.submit(self.execute, run_id)

    def execute(self, run_id: str) -> dict:
        """Run (or resume) and finish one run. A crash inside the run is recorded as a failed run, not lost.

        If this server cannot build the run (paid calls off, key or price missing, scenario gone), the run is not failed:
        it is marked recovering and stays PENDING, so DBOS resumes it at a start that can build it (CR-2 M3).
        """
        with self._active_cv:
            self._active.add(run_id)
        try:
            entry = self._execute(run_id)
            self._folder(run_id)
            self.on_finished(run_id)
            return entry
        finally:
            with self._active_cv:
                self._active.discard(run_id)
                self._active_cv.notify_all()

    def _execute(self, run_id: str) -> dict:
        try:
            parts = self.builder(run_id)
        except ApiError as e:
            self._hold(run_id, e.code)
            raise RunInterrupted from e
        self.writer.submit(lambda conn: conn.execute("DELETE FROM held WHERE run_id = ?", (run_id,))).result()
        sink = StoreSink(self.writer)
        try:
            return resume_or_start(self.db, self.writer, parts, self.runs_dir, now=self.now, sink=sink, cache_path=self.cache_path)
        except Exception as e:  # noqa: BLE001
            if self._closing:  # the writer or the call cache closed under the run: not a run failure
                raise RunInterrupted from e
            prior = self.db.events(run_id) + sink.pending  # the failed round's calls were made (and may be paid): keep them
            if not prior or prior[-1].type == "run_ended":
                raise
            rec = Recorder(run_id, [sink], now=self.now, prior=prior)
            t = max(ev.t for ev in prior)
            error = e.code if isinstance(e, (ApiError, EndpointError)) else type(e).__name__
            log.error("run %s failed: %s: %s", run_id, type(e).__name__, redact(str(e)))
            detail = error_detail(e, error)
            rec.emit(RunEndedEvent, t, status="failed", end_reason="error", episode_hash=rec.episode_hash, error=error, error_detail=detail)
            return finish_run(self.db, self.writer, run_id, self.runs_dir, self.cache_path)

    def _folder(self, run_id: str) -> None:
        """runs/<run_id>/ with the whole bundle (DECISIONS-20 §1). Like the trail, a copy of what the databases hold: a
        failure is logged and never fails the run, and the export builds the same bytes from the databases."""
        if self.cache_path is None:
            return
        try:
            write_run_folder(self.db, self.runs_dir, self.cache_path, run_id)
        except Exception as e:  # noqa: BLE001
            log.error("run folder of %s not written: %s: %s", run_id, type(e).__name__, redact(str(e)))

    def _hold(self, run_id: str, code: str) -> None:
        log.warning("run %s cannot resume on this server (%s); it stays pending", run_id, code)
        at = self.now()
        self.writer.submit(
            lambda conn: conn.execute("INSERT OR REPLACE INTO held (run_id, code, at) VALUES (?, ?, ?)", (run_id, code, at))
        ).result()
        prior = self.db.events(run_id)
        last = next((e for e in reversed(prior) if e.type == "run_status_changed"), None)
        if prior and prior[-1].type != "run_ended" and not (last and last.status == "recovering" and last.reason == code):
            t = max((ev.t for ev in prior if ev.type == "settled"), default=prior[-1].t)
            Recorder(run_id, [StoreSink(self.writer)], now=self.now, prior=prior).emit(
                RunStatusChangedEvent, t, status="recovering", reason=code
            )

    # Control

    def control(self, run_id: str, action: str) -> None:
        run = self.db.run(run_id)
        if run is None and self.db.run_config(run_id) is None:
            raise ApiError(404, "not_found", f"Run {run_id} not found.")
        if run is not None and run["ended_at"] is not None:
            raise ApiError(409, "run_not_active", f"Run {run_id} has ended.")
        self._controls.setdefault(run_id, Control()).set({"pause": "pause", "resume": "go", "stop": "stop"}[action])

    def wait(self, run_id: str, timeout: float = 30) -> dict:
        """The ledger entry once the run has ended. Under DBOS this also covers a run this process recovered, not started."""
        if not self.use_dbos:
            return self._futures[run_id].result(timeout=timeout)
        deadline = time.monotonic() + timeout
        while durable.retrieve(run_id).get_status().status not in DONE:
            if time.monotonic() > deadline:
                raise TimeoutError(run_id)
            time.sleep(0.05)
        return durable.retrieve(run_id).get_result()

    def close(self, timeout: float = SHUTDOWN_WAIT_S) -> None:
        """Under DBOS a live run stops at its next round boundary with no run_ended and stays PENDING (Q4b); DBOS resumes it.

        Waits up to `timeout` for live runs to reach that boundary, so the writer and the call cache close after them
        (a paid reply that arrives late is still cached). A run still live after that is interrupted when they close.
        """
        self._closing = True
        for c in self._controls.values():
            c.set("shutdown" if self.use_dbos else "stop")
        with self._active_cv:
            if not self._active_cv.wait_for(lambda: not self._active, timeout=timeout):
                log.warning("shutdown: runs %s still in a round after %s s", sorted(self._active), timeout)
        self._pool.shutdown(wait=True, cancel_futures=False)


def dbos_path(data_dir: Path) -> Path:
    return Path(os.environ.get("SIM_DBOS_PATH", Path(data_dir) / "dbos.sqlite"))


def _cell_used(condition_id: str, seed: int, run_id: str) -> ApiError:
    return ApiError(
        409,
        "test_seed_already_run",
        f"Test cell {condition_id} seed {seed} is used by run {run_id}. A test cell runs once; rerun it as exploratory.",
    )
