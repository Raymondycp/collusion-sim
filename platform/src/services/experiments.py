"""Experiments (DECISIONS-10 §3): conditions × seeds × a branch plan, with one batch budget.

An experiment records every run it will make before it starts any: one parent per cell (condition, seed) with its
resolved config, and the planned branch children of that parent with their requests, each with a fixed run id.
`advance` is the only step that starts work, and it reads only this record (a condition or scenario file that changes
later cannot change the plan). It stores and launches a planned parent whose config is missing, and starts a cell's
branches once its parent completed. It is idempotent, so it runs after every finished run and at server start (crash
recovery), and never re-runs a run that exists. A cell that cannot move on is marked blocked with its error code.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from adapters.llm.budget import BudgetGuard
from adapters.store import RunsDB, Writer, clear_experiment_block, put_experiment, put_experiment_block, put_experiment_runs
from services import estimate as est
from services import read
from services.configs import AgentConfig, ApiError, BatchBudget, BudgetSettings, ClockSettings, FieldError, MemorySettings, Model, RunConfig
from services.profiles import ProfileService
from services.research import Condition, Research, canonical_hash
from services.runner import RunService, new_id
from simcore.branch import BranchRequest
from simcore.run import RunSpec
from simschema.events import AblationIntervention, PlaceboIntervention, ProbeIntervention

log = logging.getLogger("sim.experiments")

ENDED = ("completed", "failed", "stopped")


class ProbePlan(Model):
    at: int = Field(20, ge=2)
    rounds: int = Field(5, ge=1)
    target: str = "B"


class PlaceboPlan(Model):
    at: int = Field(20, ge=2)
    rounds: int = Field(5, ge=1)
    n: int = Field(3, ge=1, le=10)


class AblationPlan(Model):
    at: int = Field(10, ge=2)
    mode: Literal["clear", "shuffle"] = "shuffle"
    groups: list[str] = Field(
        default_factory=lambda: ["C1", "C2a", "C2b", "C3", "C4", "LC"],
        description="Condition groups that get ablation children (default: every group, so the feature is never missing by group)",
    )


class BranchPlan(Model):
    """Plan §11: probe at round 20 (1 probe + 3 placebo, 5 rounds each); ablation from round 10 to the parent's end, every
    group (a null ablation where the condition has no private channel)."""

    probe: ProbePlan | None = Field(default_factory=ProbePlan)
    placebo: PlaceboPlan | None = Field(default_factory=PlaceboPlan)
    ablation: AblationPlan | None = Field(default_factory=AblationPlan)


class ExperimentCreate(Model):
    name: str | None = Field(None, max_length=120)
    conditions: list[str] = Field(min_length=1)
    split: Literal["pilot", "dev", "test"]
    seeds: list[int] = Field(min_length=1)
    agents: list[AgentConfig] = Field(min_length=2)
    agents_by_condition: dict[str, list[AgentConfig]] = Field(
        default_factory=dict, description="Agents for one condition instead of `agents` (C2b needs a different model family per seller)"
    )
    clock: ClockSettings = Field(default_factory=lambda: ClockSettings(mode="sync"))
    memory: MemorySettings = Field(default_factory=MemorySettings)
    budget: BudgetSettings = Field(description="Per run: every parent and every branch child")
    batch_budget: BatchBudget
    branch_plan: BranchPlan = Field(default_factory=BranchPlan)
    opening_price: float | None = Field(None, ge=0, description="RunConfig.opening_price for every run of the experiment")
    replicate: int = Field(0, ge=0, description="RunConfig.replicate for every run of the experiment (a rerun makes new calls)")

    @model_validator(mode="after")
    def _distinct(self) -> "ExperimentCreate":
        if len(set(self.conditions)) != len(self.conditions):
            raise FieldError("conditions", "conditions must be distinct")
        if len(set(self.seeds)) != len(self.seeds):
            raise FieldError("seeds", "seeds must be distinct")
        if set(self.agents_by_condition) - set(self.conditions):
            raise FieldError("agents_by_condition", "agents_by_condition names a condition that is not in conditions")
        return self

    def hash(self) -> str:
        drop = {"name"} | ({"opening_price"} if self.opening_price is None else set()) | ({"replicate"} if self.replicate == 0 else set())
        return canonical_hash(self.model_dump(mode="json", exclude=drop))  # a null opening_price keeps stored request hashes

    def run_config(self, condition_id: str, seeds: list[int] | None = None) -> RunConfig:
        return RunConfig(
            kind="research",
            name=self.name,
            condition_id=condition_id,
            split=self.split,
            seeds=seeds or self.seeds,
            agents=self.agents_by_condition.get(condition_id, self.agents),
            clock=self.clock,
            memory=self.memory,
            budget=self.budget,
            batch_budget=self.batch_budget,
            opening_price=self.opening_price,
            replicate=self.replicate,
        )


# Plan: the runs of one cell


def _branch_requests(plan: BranchPlan, cond: Condition, stop: int) -> Iterator[tuple[str, BranchRequest]]:
    """(kind, request) for one cell. Ablation: one child per private channel, or one null ablation without one, from `at`
    to the parent's last round."""
    if plan.probe:
        p = plan.probe
        yield "probe", BranchRequest(at=p.at, rounds=p.rounds, intervention=ProbeIntervention(target=p.target))
    if plan.placebo:
        p = plan.placebo
        yield "placebo", BranchRequest(at=p.at, rounds=p.rounds, n=p.n, intervention=PlaceboIntervention())
    if plan.ablation and cond.file.group in plan.ablation.groups and not cond.file.channels.private_pairs:
        a = plan.ablation
        yield "ablation", BranchRequest(at=a.at, rounds=stop - a.at + 1, intervention=AblationIntervention(ablated="none"))
    elif plan.ablation and cond.file.group in plan.ablation.groups:
        a, sellers = plan.ablation, sorted(cond.file.sellers)
        for i, x in enumerate(sellers):
            for y in sellers[i + 1 :]:
                yield (
                    "ablation",
                    BranchRequest(
                        at=a.at, rounds=stop - a.at + 1, intervention=AblationIntervention(channel_id=f"private_{x}_{y}", mode=a.mode)
                    ),
                )


def _check_plan(body: ExperimentCreate, conds: dict[str, Condition], stops: dict[int, int]) -> None:
    errors = []
    for cid, cond in conds.items():
        for seed, stop in stops.items():
            for kind, req in _branch_requests(body.branch_plan, cond, stop):
                if req.at + req.rounds - 1 > stop:
                    errors.append(
                        {
                            "path": f"branch_plan/{kind}",
                            "message": f"{cid} seed {seed}: rounds {req.at}–{req.at + req.rounds - 1} pass the last round {stop}",
                        }
                    )
    probe = body.branch_plan.probe
    if probe and any(probe.target not in {a.id for a in body.agents_by_condition.get(c, body.agents) if a.role == "seller"} for c in conds):
        errors.append({"path": "branch_plan/probe/target", "message": "not a seller"})
    if errors:
        raise ApiError(422, "validation_error", "The branch plan does not fit every cell.", errors[:20])


class ExperimentService:
    def __init__(
        self,
        db: RunsDB,
        writer: Writer,
        runs: RunService,
        research: Research,
        profiles: ProfileService | None = None,
        experiments_dir: Path | None = None,
    ) -> None:
        """`experiments_dir`: where experiments/<id>/ is written (DECISIONS-20 §2); None writes no folder."""
        self.db, self.writer, self.runs, self.research, self.profiles = db, writer, runs, research, profiles
        self.experiments_dir = experiments_dir
        self._lock = threading.RLock()
        self._guards: dict[str, BudgetGuard] = {}
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._work, name="experiments", daemon=True)
        self._worker.start()
        runs.on_finished = self._finished
        runs.batch_guard = self.guard

    def close(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=10)

    # Create

    def _plan(self, body: ExperimentCreate) -> tuple[dict[str, Condition], dict[int, int], dict[tuple[str, int], RunConfig]]:
        """Every unit-1 run check per condition (fields, split, seeds, model rule, scenario hashes), without the paid-call
        gate, so an estimate works while paid calls are off. Returns the conditions, each seed's stop round and each
        cell's bound config."""
        conds, stops, cells = {}, {}, {}
        for i, cid in enumerate(body.conditions):
            try:
                conds[cid] = self.research.condition(cid)
            except ApiError as e:
                if e.status != 404:
                    raise
                raise ApiError(422, "validation_error", e.message, [{"path": f"conditions/{i}", "message": "unknown condition"}]) from None
            for r in self.research.expand(body.run_config(cid)):
                self.runs.check_agents(r.config, r.scenario)
                if body.split == "test":
                    self.runs.check_test_cells([(cid, r.config.seeds[0])])
                stops[r.config.seeds[0]] = r.scenario.horizon.stop_round(r.config.seeds[0])
                cells[cid, r.config.seeds[0]] = r.config
        _check_plan(body, conds, stops)
        return conds, stops, cells

    def estimate(self, body: ExperimentCreate) -> dict:
        conds, stops, cells = self._plan(body)
        per_condition, missing, per_profile = [], set(), {}
        totals = {"calls": 0, "input": 0, "output": 0}
        usd_total: float | None = 0.0
        runs = {"parents": 0, "probe": 0, "placebo": 0, "ablation": 0}
        for cid, cond in conds.items():
            usage: est.Usage = {}
            n_runs = 0
            for seed, stop in stops.items():
                rounds = [("parents", 1, stop)] + [
                    (k, r.at, r.rounds) for k, r in _branch_requests(body.branch_plan, cond, stop) for _ in range(r.n)
                ]
                for kind, first, n_rounds in rounds:
                    runs[kind] += 1
                    n_runs += 1
                    for key, row in est.run_usage(cells[cid, seed], n_rounds, first).items():
                        est.add(usage, key, *row)
            usd, gaps = est.usd(usage, self.price_of)
            missing |= gaps
            for (pid, _), r in usage.items():
                per_profile[pid] = per_profile.get(pid, 0) + r[0]
            calls, inp, out = est.totals(usage)
            row = {
                "condition_id": cid,
                "runs": n_runs,
                "calls_max": calls,
                "input_tokens_max": inp,
                "output_tokens_max": out,
                "usd_max": usd,
            }
            per_condition.append(row)
            totals["calls"] += row["calls_max"]
            totals["input"] += row["input_tokens_max"]
            totals["output"] += row["output_tokens_max"]
            usd_total = None if usd_total is None or row["usd_max"] is None else usd_total + row["usd_max"]
        bb = body.batch_budget
        checks = [totals["calls"] <= bb.max_calls] if bb.max_calls is not None else []
        if bb.max_tokens is not None:
            checks.append(totals["input"] + totals["output"] <= bb.max_tokens)
        if bb.max_usd is not None:
            checks.append(None if usd_total is None else usd_total <= bb.max_usd)
        llm = self.runs.llm
        windows, warnings = est.window_rows(per_profile, llm.window_status if llm else lambda _p: None)
        if missing and bb.max_calls is None and bb.max_tokens is None:
            warnings.append(
                f"Profiles {sorted(missing)} have no price and the batch budget has only max_usd: "
                "the start is refused (budget_unenforceable)."
            )
        return {
            "n_cells": len(conds) * len(stops),
            "runs": runs | {"total": sum(runs.values())},
            "calls_max": totals["calls"],
            "tokens_max": {"input": totals["input"], "output": totals["output"]},
            "usd_max": None if usd_total is None else round(usd_total, 6),
            "pricing_missing_for": sorted(missing),
            "per_condition": per_condition,
            "within_budget": None if None in checks else all(checks),
            "rate_window": windows,
            "warnings": warnings,
            "assumptions": est.assumptions("the condition's"),
        }

    def price_of(self, profile_id: str, model: str) -> tuple[float, float] | None:
        return self.profiles.price_of(profile_id, model) if self.profiles else None

    def create(self, body: ExperimentCreate) -> dict:
        """Check everything, then record the whole plan (each parent's resolved config, each seed's stop round, each
        child's branch request) before any run starts. `advance` later works from this record, not from the files."""
        if self.runs.llm:  # the paid-call gate first, as for a run start
            for cid in body.conditions:
                self.runs.llm.check_allowed(body.run_config(cid))
        conds, stops, _ = self._plan(body)
        estimate = self.estimate(body)
        bb = body.batch_budget
        if estimate["pricing_missing_for"] and bb.max_calls is None and bb.max_tokens is None:
            raise ApiError(
                422,
                "budget_unenforceable",
                f"Profiles {estimate['pricing_missing_for']} have no price, so batch_budget.max_usd cannot stop their calls.",
                [{"path": "batch_budget/max_calls", "message": "required when a profile has no price"}],
            )
        parents = {cid: [new_id("run_") for _ in body.seeds] for cid in body.conditions}
        prepared = {cid: self.runs.prepare(body.run_config(cid), parents[cid]) for cid in body.conditions}
        exp_id, created = new_id("exp_"), self.runs.now()
        rows, plans = [], {}
        for cid, cond in conds.items():
            for seed, rid in zip(body.seeds, parents[cid]):
                rows.append((cid, seed, "parent", 0, rid))
                plans[rid] = []
                for pos, (kind, req) in enumerate(_branch_requests(body.branch_plan, cond, stops[seed])):
                    rows += [(cid, seed, kind, pos * 10 + j, new_id("run_")) for j in range(req.n)]
                    plans[rid].append({"kind": kind, "pos": pos, "request": req.model_dump(mode="json")})
        doc = {
            "request": body.model_dump(mode="json"),
            "estimate": estimate,
            "stops": {str(seed): stop for seed, stop in stops.items()},
            "parents": {
                spec.run_id: {"config": cfg.model_dump_json(exclude_none=True), "spec": spec.model_dump_json()}
                for p in prepared.values()
                for cfg, spec in p
            },
            "branches": plans,
        }
        with self._lock:  # a parent that ends during the commits must not make `advance` launch the later parents too
            put_experiment(self.writer, exp_id, body.hash(), json.dumps(doc), created)
            put_experiment_runs(self.writer, exp_id, rows)
            for p in prepared.values():
                self.runs.commit(p)
        self._folder(exp_id)
        return self.detail(exp_id)

    # Advance (idempotent)

    def _finished(self, run_id: str) -> None:
        exp_id = self.db.experiment_of(run_id)
        if exp_id:
            self._queue.put(exp_id)

    def _work(self) -> None:
        while (exp_id := self._queue.get()) is not None:
            try:
                self.advance(exp_id)
            except Exception:  # noqa: BLE001
                log.exception("experiment %s: advance failed", exp_id)
            self._folder(exp_id)

    def _folder(self, exp_id: str) -> None:
        """experiments/<id>/ at create, at retry and after one of its runs finished (DECISIONS-20 §2): the status changes
        only there, since a stop or a budget stop ends runs. A failure is logged, never raised."""
        if self.experiments_dir is None:
            return
        try:
            read.write_experiment_folder(self.runs.runs_dir, self.experiments_dir, self.detail(exp_id))
        except Exception:  # noqa: BLE001
            log.exception("experiment %s: folder not written", exp_id)

    def recover(self) -> None:
        """At server start: launch every stored run of an experiment that has not ended, then advance. With DBOS the
        workflow id is the run id, so a run that DBOS already resumes is not started twice."""
        for exp in self.db.experiments():
            for r in self.db.experiment_runs(exp["id"]):
                run = self.db.run(r["run_id"])
                if self.db.run_config(r["run_id"]) is not None and (run is None or run["ended_at"] is None):
                    self.runs.launch(r["run_id"])
            self.advance(exp["id"])

    def advance(self, exp_id: str) -> None:
        """Store and launch recorded parents that have no config; branch completed parents whose children have no config.
        Uses only what `create` recorded. A cell that cannot move on is marked blocked with the error; the others go on."""
        with self._lock:
            doc = self._doc(exp_id)
            blocks = self.db.experiment_blocks(exp_id)
            for cell, ids in sorted(_cells(self.db.experiment_runs(exp_id)).items()):
                try:
                    self._advance_cell(doc, ids)
                except Exception as e:  # noqa: BLE001
                    code, message = (e.code, e.message) if isinstance(e, ApiError) else ("internal_error", type(e).__name__)
                    if not isinstance(e, ApiError):
                        log.exception("experiment %s cell %s", exp_id, cell)
                    if blocks.get(cell, {}).get("code") != code:
                        put_experiment_block(self.writer, exp_id, cell, code, message, self.runs.now())
                else:
                    if cell in blocks:
                        clear_experiment_block(self.writer, exp_id, cell)

    def retry(self, exp_id: str) -> dict:
        """After the cause of a block is fixed: forget every block and advance. A cell that still cannot move on is
        blocked again, with the new error and time. Runs that ended (failed, stopped) are not run again."""
        with self._lock:
            self._doc(exp_id)
            for cell in self.db.experiment_blocks(exp_id):
                clear_experiment_block(self.writer, exp_id, cell)
            self.advance(exp_id)
        self._folder(exp_id)
        return self.detail(exp_id)

    def _advance_cell(self, doc: dict, cell: dict) -> None:
        parent = cell["parent"]
        if self.db.run_config(parent) is None:
            stored = doc["parents"][parent]
            self.runs.commit([(RunConfig.model_validate_json(stored["config"]), RunSpec.model_validate_json(stored["spec"]))])
            return
        run = self.db.run(parent)
        if run is None or run["status"] != "completed":
            return
        for plan in doc["branches"][parent]:
            req = BranchRequest.model_validate(plan["request"])
            ids = [cell["children"][(plan["kind"], plan["pos"] * 10 + j)] for j in range(req.n)]
            if any(self.db.run_config(i) is None for i in ids):
                self.runs.branch(parent, req, ids=ids)

    def _doc(self, exp_id: str) -> dict:
        row = self.db.experiment(exp_id)
        if row is None:
            raise ApiError(404, "not_found", f"Experiment {exp_id} not found.")
        return json.loads(row["body_json"])

    def _body(self, exp_id: str) -> ExperimentCreate:
        return ExperimentCreate.model_validate(self._doc(exp_id)["request"])

    # Batch budget

    def guard(self, run_id: str) -> BudgetGuard | None:
        exp_id = self.db.experiment_of(run_id)
        if exp_id is None:
            return None
        with self._lock:
            if exp_id not in self._guards:
                bb = self._body(exp_id).batch_budget
                g = BudgetGuard(max_usd=bb.max_usd, max_calls=bb.max_calls, max_tokens=bb.max_tokens)
                spent = self._spent(exp_id)
                g.calls, g.spent_usd, g.tokens, g.unpriced = spent["calls"], spent["usd"], spent["tokens"], spent["unpriced"]
                self._guards[exp_id] = g
            return self._guards[exp_id]

    def _spent(self, exp_id: str) -> dict:
        return self.db.paid_calls([r["run_id"] for r in self.db.experiment_runs(exp_id)])

    # Read

    def detail(self, exp_id: str) -> dict:
        doc = self._doc(exp_id)
        row = self.db.experiment(exp_id)
        rows = self.db.experiment_runs(exp_id)
        blocks = self.db.experiment_blocks(exp_id)
        mine = {r["run_id"] for r in rows}
        summaries = {r["run_id"]: read.summary(r) for r in self.db.runs() if r["run_id"] in mine}
        cells = []
        for key, cell in sorted(_cells(rows).items()):
            parent = _run_state(summaries.get(cell["parent"]), cell["parent"])
            children = [{"kind": k, "index": i, **_run_state(summaries.get(rid), rid)} for (k, i), rid in sorted(cell["children"].items())]
            block = blocks.get(key)
            status = "blocked" if block else _cell_status(parent, children)
            error = {"code": block["code"], "message": block["message"], "at": block["at"]} if block else None
            cells.append({"condition_id": key[0], "seed": key[1], "status": status, "error": error, "parent": parent, "branches": children})
        counts: dict[str, int] = {}
        for c in cells:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        live = set(counts) - set(ENDED) - {"blocked"}
        status = "running" if live else "blocked" if "blocked" in counts else "completed" if set(counts) == {"completed"} else "ended"
        spent = self._spent(exp_id)
        req = doc["request"]
        return {
            "id": exp_id,
            "name": req.get("name"),
            "hash": row["hash"],
            "created_at": row["created_at"],
            "status": status,
            "split": req["split"],
            "conditions": req["conditions"],
            "seeds": req["seeds"],
            "branch_plan": req["branch_plan"],
            "batch_budget": req["batch_budget"],
            "spend": spent | {"usd": round(spent["usd"], 6), "usd_complete": spent["unpriced"] == 0},
            "counts": counts,
            "estimate": doc["estimate"],
            "cells": cells,
        }

    def summaries(self) -> list[dict]:
        out = []
        for row in self.db.experiments():
            d = self.detail(row["id"])
            out.append({k: d[k] for k in ("id", "name", "hash", "created_at", "status", "split", "conditions", "seeds", "counts", "spend")})
        return out

    def wait(self, exp_id: str, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while (d := self.detail(exp_id))["status"] == "running":
            if time.monotonic() > deadline:
                raise TimeoutError(exp_id)
            time.sleep(0.1)
        return d


def _cells(rows: list[dict]) -> dict[tuple[str, int], dict]:
    cells: dict[tuple[str, int], dict] = {}
    for r in rows:
        cell = cells.setdefault((r["condition_id"], r["seed"]), {"parent": None, "children": {}})
        if r["kind"] == "parent":
            cell["parent"] = r["run_id"]
        else:
            cell["children"][(r["kind"], r["idx"])] = r["run_id"]
    return cells


def _run_state(summary: dict | None, run_id: str) -> dict:
    if summary is None:
        return {"run_id": run_id, "status": "planned", "end_reason": None}
    return {"run_id": run_id, "status": summary["status"], "end_reason": summary["end_reason"]}


def _cell_status(parent: dict, children: list[dict]) -> str:
    if parent["status"] != "completed":
        return parent["status"]
    states = {c["status"] for c in children}
    if states - set(ENDED):
        return "branching"
    return "failed" if "failed" in states else "stopped" if "stopped" in states else "completed"
