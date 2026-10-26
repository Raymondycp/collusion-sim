"""Judge jobs (item 3 unit J2): plan, estimate, store, run and read evaluations (DECISIONS-9 §5–§8, §14, §18).

Checks at plan time, in this order: paid switch and the judge ledger chain, episodes (exist, parent, completed), split
allowed by the block, freeze gates (dev: stage conditions; test: stage rule and the test judgement locks), item set, view
in block, judge family, ledger room. The ledger room is checked again inside the transaction that stores the job. One lock
over every backend, so one judge call runs at a time (CR-21 F3)."""

from __future__ import annotations

import json
import logging
import queue
import statistics
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Literal

import yaml
from pydantic import Field, model_validator

from adapters.llm.endpoint import Endpoint, EndpointError, Reply
from adapters.store import RunsDB, Writer
from judge import batch, items, llm_judge, scoring, scripted
from judge.blocks import BLOCKS, Block
from judge.items import ItemSet
from judge.store import BudgetExceeded, EvalDB, JudgementUsed, PlannedCall, append_call, put_evaluation, reopen, set_status
from judge.verify import check
from judge.views import Rendered, ViewId
from services.configs import ApiError, Model, RunConfig
from services.imports import is_imported
from services.judging import JudgeReads
from services.llm_runs import LlmRuns
from services.profiles import paid_calls_allowed
from services.research import RESEARCH_FILES, Research
from simschema.events import EpisodeHeader

log = logging.getLogger("sim.evaluations")

Backend = Literal["llm", "jev", "scripted"]
JEV_FAMILY = "jev"
BUILT_BACKENDS = ("scripted", "llm")
"""Jev is designed, not built (lead, 21 Oct scope cut)."""
ERRORS_IN_A_ROW = 2
"""Consecutive `error` results of one kind that stop a job, so a systemic fault spends at most this many calls (CR-36 m2)."""
PAID_OFF = "Judge backends llm and jev make paid calls. The server runs with paid calls off."
AGREEMENT_RULE = "llm majority vote equals (jev mean P(agreement) >= 0.5), over episodes both judged ok"


class LlmJudge(Model):
    provider_profile_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_family: str = Field(min_length=1, description="Must differ from every seller's family")


class JevJudge(Model):
    provider_profile_id: str = Field(min_length=1)
    model: str = Field(pattern=r"^jev-\d+(\.\d+)*(-free)?$", description="A fixed version, never a latest alias")


class ScriptedJudgeSettings(Model):
    pass


class Backends(Model):
    llm: LlmJudge | None = None
    jev: JevJudge | None = None
    scripted: ScriptedJudgeSettings | None = Field(None, description="Rules only, for tests; never a feature column")

    def ids(self) -> list[str]:
        return [b for b in ("llm", "jev", "scripted") if getattr(self, b) is not None]


class EvaluationCreate(Model):
    run_ids: list[str] | None = Field(None, min_length=1, max_length=200)
    experiment_id: str | None = None
    block: Literal["wording", "main", "support_one_wording", "talk_prices", "stability", "robustness", "imported"]
    view: ViewId
    item_set_id: str = Field(min_length=1)
    backends: Backends
    order_seed: int | None = Field(None, ge=0, le=2**32 - 1)

    @model_validator(mode="after")
    def _shape(self) -> "EvaluationCreate":
        if (self.run_ids is None) == (self.experiment_id is None):
            raise ValueError("give run_ids or experiment_id, not both")
        if not self.backends.ids():
            raise ValueError("give at least one backend")
        return self


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class EvaluationService:
    def __init__(self, db: RunsDB, eval_db: EvalDB, writer: Writer, research: Research, reads: JudgeReads, new_id, llm: LlmRuns) -> None:
        """llm: the profiles, the key store, the call cache and (tests only) the fake transport of the LLM runs."""
        self.db, self.eval_db, self.writer, self.research, self.reads, self.new_id = db, eval_db, writer, research, reads, new_id
        self.llm = llm
        self._call_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._work, name="evaluations", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=10)

    # Plan

    def _run_ids(self, body: EvaluationCreate) -> list[str]:
        if body.run_ids is not None:
            return list(dict.fromkeys(body.run_ids))
        if self.db.experiment(body.experiment_id) is None:
            raise ApiError(404, "not_found", f"Experiment {body.experiment_id} not found.")
        return [r["run_id"] for r in self.db.experiment_runs(body.experiment_id) if r["kind"] == "parent"]

    def _skipped(self, body: dict, plan: list[PlannedCall]) -> list[dict]:
        """The experiment's parents that a job did not judge. create refuses every other exclusion, so each was skipped for
        its condition."""
        if body.get("experiment_id") is None:
            return []
        judged = {p.run_id for p in plan}
        parents = [r["run_id"] for r in self.db.experiment_runs(body["experiment_id"]) if r["kind"] == "parent"]
        return [{"run_id": rid, "reason": "condition_not_in_block"} for rid in parents if rid not in judged]

    def _episode(self, run_id: str, block: Block) -> tuple[str | None, str | None]:
        """(split, None) for a run the block may judge, or (None, reason)."""
        row = self.db.run(run_id)
        if self.db.run_config(run_id) is None:
            return None, "not_found"
        if row is None or row["status"] != "completed":
            return None, "not_completed"
        if row["parent_run_id"] is not None:
            return None, "not_parent"
        if row["split"] not in block.splits:
            return None, "split_not_in_block"
        if block.conditions is not None and EpisodeHeader.model_validate_json(row["header_json"]).condition_id not in block.conditions:
            return None, "condition_not_in_block"
        return row["split"], None

    def _families(self, run_id: str) -> set[str | None]:
        """The model families of the run's LLM sellers; None for a seller that declares none (CR-33 m4)."""
        cfg = RunConfig.model_validate_json(self.db.run_config(run_id)["config_json"])
        if is_imported(cfg):  # the families given at POST /imports
            return {a.policy.params.get("model_family") or None for a in cfg.agents}
        return {a.policy.llm.model_family or None for a in cfg.agents if a.policy.llm is not None}

    def _family_check(self, episodes: list[tuple[str, str]], backend: str, family: str) -> None:
        unknown = [rid for rid, _ in episodes if None in self._families(rid)]
        if unknown:
            raise ApiError(
                422,
                "judge_same_family",
                f"An LLM seller in {', '.join(unknown)} has no model_family, so the {backend} judge cannot be shown to be of"
                " another family. Set model_family in the run config.",
                [{"path": f"backends/{backend}", "message": "a seller has no model_family"}],
            )
        clash = [rid for rid, _ in episodes if family in self._families(rid)]
        if clash:
            raise ApiError(
                422,
                "judge_same_family",
                f"The {backend} judge family {family} is a seller family in {', '.join(clash)}.",
                [{"path": "backends/llm/model_family" if backend == "llm" else "backends/jev", "message": "same family as a seller"}],
            )

    def _item_set(self, item_set_id: str) -> tuple[ItemSet, str]:
        listed = self.reads.item_sets()
        if not listed or not listed[0]["valid"] or listed[0]["id"] != item_set_id:
            problems = listed[0]["problems"] if listed else [{"path": "file", "message": "conditions/judge-rubric.yaml is missing"}]
            raise ApiError(
                422,
                "validation_error",
                f"Item set {item_set_id} is not the valid item set in conditions/judge-rubric.yaml.",
                [{"path": "item_set_id", "message": p["path"] + ": " + p["message"]} for p in problems]
                or [{"path": "item_set_id", "message": "unknown id"}],
            )
        return self._item_set_model(listed[0]["hash"])

    def _item_set_model(self, want_hash: str) -> tuple[ItemSet, str]:
        """The item set of the rubric file, which must still have the hash of the plan (CR-33 a)."""
        stored = self.research.stored_file(RESEARCH_FILES["judge_rubric"])
        if stored["hash"] != want_hash:
            raise ApiError(
                409,
                "hash_mismatch",
                f"conditions/judge-rubric.yaml changed after the job was planned (hash {stored['hash']}, planned {want_hash}).",
            )
        item_set, problems = items.parse(yaml.safe_load(stored["text"] or "null"))
        if item_set is None:
            raise ApiError(409, "conflict", "conditions/judge-rubric.yaml is no longer a valid item set.", problems)
        return item_set, want_hash

    def plan(
        self, body: EvaluationCreate, evaluation_id: str
    ) -> tuple[list[PlannedCall], list[dict], list[dict], list[dict], ItemSet, str, int]:
        """(plan, excluded, skipped, ledger rows, item set, item set hash, order seed). Raises the first check that fails; the
        excluded episodes are returned, not raised, so the estimate can list them. For an experiment_id request, a parent whose
        condition the block does not judge is skipped, not excluded (CR-64 finding 1): one experiment holds every condition
        of the plan, and each block takes its own."""
        backends = body.backends.ids()
        if {"llm", "jev"} & set(backends) and not self._paid_ok():
            raise ApiError(409, "paid_calls_disabled", PAID_OFF)
        if {"llm", "jev"} & set(backends):
            self._chain_intact()
        block = BLOCKS[body.block]
        episodes, excluded, skipped = [], [], []
        for i, rid in enumerate(self._run_ids(body)):
            split, reason = self._episode(rid, block)
            if reason == "condition_not_in_block" and body.experiment_id is not None:
                skipped.append({"run_id": rid, "reason": reason})
            elif reason:
                excluded.append({"run_id": rid, "index": i, "reason": reason})
            else:
                episodes.append((rid, split))
        splits = {s for _, s in episodes}
        if "dev" in splits:
            self.research.check_dev_open()
        if "test" in splits:
            self.research.check_test_open()
        if body.backends.llm is not None and splits & {"dev", "test"}:
            self.research.check_judge_frozen(body.backends.llm)
        item_set, item_hash = self._item_set(body.item_set_id)
        if body.view not in block.views:
            raise ApiError(
                422,
                "validation_error",
                f"Block {body.block} judges views {', '.join(block.views)}.",
                [{"path": "view", "message": f"not in block {body.block}"}],
            )
        if body.backends.llm is not None:
            self._family_check(episodes, "llm", body.backends.llm.model_family)
        if body.backends.jev is not None:
            self._family_check(episodes, "jev", JEV_FAMILY)
        if unbuilt := [b for b in backends if b not in BUILT_BACKENDS]:
            raise ApiError(
                422,
                "protocol_not_built",
                f"Judge backend {', '.join(unbuilt)} is designed, not built yet.",
                [{"path": f"backends/{b}", "message": "not built"} for b in unbuilt],
            )
        if (llm := body.backends.llm) is not None:
            self.llm.profiles.bind(llm.provider_profile_id, llm.model, llm.model_family)  # 404, 422 key or protocol
        seed = body.order_seed if body.order_seed is not None else batch.default_order_seed(evaluation_id)
        calls = batch.plan(episodes, body.block, body.view, backends, seed)
        for p in calls:
            if p.split == "test" and (owner := self.eval_db.judgement(p.cell)) is not None:
                raise _judged(p, owner)
        rows = batch.room(self.eval_db.ledger_state(), calls)
        return calls, excluded, skipped, rows, item_set, item_hash, seed

    def _paid_ok(self) -> bool:
        """The paid switch, as for LLM runs: on with SIM_ALLOW_PAID=1, or with the tests' fake transport."""
        return self.llm.transport is not None or paid_calls_allowed(self.llm.profiles.keys.env_file)

    def _chain_intact(self) -> None:
        ok, bad = self.eval_db.verify()
        if not ok:
            raise ApiError(409, "conflict", f"The judge ledger hash chain is broken at line {bad}. Paid judge calls are refused.")

    def estimate(self, body: EvaluationCreate) -> dict:
        calls, excluded, skipped, rows, _, _, seed = self.plan(body, "estimate")
        per_backend = {}
        for b in body.backends.ids():
            mine = [p for p in calls if p.backend == b]
            tokens = sum(self.reads.rendered(p.run_id, p.view, p.transform).tokens_est for p in mine)
            row = next((r for r in rows if r["backend"] == b), None)
            per_backend[b] = {
                "calls": len(mine),
                "tokens_in_est": tokens,
                "ledger": row,
                "within_cap": row is None or batch.over_cap([row]) is None,
            }
        warnings = [] if not (problem := batch.over_cap(rows)) else [problem]
        return {
            "episodes": len({p.run_id for p in calls}),
            "excluded": excluded,
            "skipped": skipped,
            "order_seed": seed if body.order_seed is not None else None,
            "per_backend": per_backend,
            "warnings": warnings,
            "assumptions": ["A cache hit costs no ledger call; the estimate counts every planned call."],
        }

    # Create and run

    def create(self, body: EvaluationCreate) -> dict:
        eid = self.new_id("eval_")
        calls, excluded, _, rows, _, item_hash, seed = self.plan(body, eid)
        if excluded:
            first = excluded[0]
            raise ApiError(
                422,
                "validation_error",
                f"Run {first['run_id']} cannot be judged in this block: {first['reason']}.",
                [{"path": f"run_ids/{e['index']}", "message": e["reason"]} for e in excluded],
            )
        if not calls:
            raise ApiError(422, "validation_error", "No episode to judge.", [{"path": "run_ids", "message": "empty"}])
        if problem := batch.over_cap(rows):
            raise ApiError(409, "evaluation_budget_exceeded", problem[0].upper() + problem[1:] + ".")
        try:
            put_evaluation(
                self.writer,
                eid,
                body.model_dump(mode="json", exclude_none=True),
                item_hash,
                seed,
                calls,
                _now(),
                lambda state: batch.over_cap(batch.room(state, calls)),
            )
        except BudgetExceeded as e:
            raise ApiError(409, "evaluation_budget_exceeded", str(e)[0].upper() + str(e)[1:] + ".") from None
        except JudgementUsed as e:
            raise _judged(next(p for p in calls if p.cell == e.cell), e.evaluation_id) from None
        self._queue.put(eid)
        return self.detail(eid)

    def recover(self) -> None:
        """At server start: run on every job that has not ended. Calls with a record are not made again. A failed job
        waits for POST /evaluations/{id}/resume."""
        ok, bad = self.eval_db.verify()
        if not ok:
            log.error("judge ledger hash chain broken at line %s; paid judge calls are refused", bad)
        for row in self.eval_db.evaluations():
            if row["status"] not in ("queued", "running"):
                continue
            if _paid(row) and not self._paid_ok():
                log.warning("evaluation %s waits: it makes paid calls and the server runs with paid calls off", row["id"])
                continue
            self._queue.put(row["id"])

    def resume(self, eid: str) -> dict:
        """A failed job runs again under the same id, like a run's crash resume: the calls with a record are kept, and its
        test locks stay its own (CR-33 b). The item set must still have the planned hash."""
        row = self.eval_db.evaluation(eid)
        if row is None:
            raise ApiError(404, "not_found", f"Evaluation {eid} not found.")
        if row["status"] != "failed":
            raise ApiError(409, "conflict", f"Evaluation {eid} is {row['status']}. Only a failed job resumes.")
        if _paid(row):
            if not self._paid_ok():
                raise ApiError(409, "paid_calls_disabled", PAID_OFF)
            self._chain_intact()
        self._item_set_model(row["item_set_hash"])
        done = self._done(eid)
        left = [p for p in self.eval_db.plan(eid) if p.idx not in done]
        try:
            reopened = reopen(self.writer, eid, lambda state: batch.over_cap(batch.room(state, left)))
        except BudgetExceeded as e:
            raise ApiError(409, "evaluation_budget_exceeded", str(e)[0].upper() + str(e)[1:] + ".") from None
        if not reopened:
            raise ApiError(409, "conflict", f"Evaluation {eid} changed state. Only a failed job resumes.")
        self._queue.put(eid)
        return self.detail(eid)

    def _work(self) -> None:
        while (eid := self._queue.get()) is not None:
            self._run_safely(eid)

    def _run_safely(self, eid: str) -> None:
        try:
            self._run(eid)
        except ApiError as e:
            set_status(self.writer, eid, "failed", _now(), {"code": e.code, "message": e.message})
        except Exception as e:  # noqa: BLE001
            set_status(self.writer, eid, "failed", _now(), {"code": "evaluation_failed", "message": type(e).__name__})

    def _run(self, eid: str) -> None:
        row = self.eval_db.evaluation(eid)
        body = json.loads(row["body_json"])
        set_status(self.writer, eid, "running", _now())
        done = self._done(eid)
        item_set, _ = self._item_set_model(row["item_set_hash"])
        endpoint = self._endpoint(eid, body) if "llm" in body["backends"] else None
        rendered: dict[tuple, Rendered] = {}
        headers: dict[str, dict] = {}
        errors: list[str] = []
        for p in self.eval_db.plan(eid):
            if p.idx in done:
                continue
            key = (p.run_id, p.view, p.transform)
            if key not in rendered:
                rendered[key] = self.reads.rendered(*key)
                headers[p.run_id] = self.db.run(p.run_id)
            with self._call_lock:
                got = self._call(eid, p, rendered[key], item_set, headers[p.run_id], endpoint)
            errors = errors + [got["error_reason"]] if got and got["status"] == "error" else []
            if len(errors) >= ERRORS_IN_A_ROW and len(set(errors[-ERRORS_IN_A_ROW:])) == 1:
                raise ApiError(
                    409,
                    "conflict",
                    f"{ERRORS_IN_A_ROW} judge calls in a row failed with {errors[-1]}. The job stops; resume it after the fix.",
                )
        set_status(self.writer, eid, "completed", _now())

    def _done(self, eid: str) -> set[int]:
        """Planned calls that are not made again: any whose last record is not an `error`. A `sent` row with no result is a
        spent call (CR-33 c); an `error` is redone at resume, with a new charged `sent` row (CR-36 m1, m2)."""
        last = {c["plan_idx"]: c["status"] for c in self.eval_db.calls(eid)}
        return {idx for idx, status in last.items() if status != "error"}

    def _endpoint(self, eid: str, body: dict) -> Endpoint:
        """One endpoint per job. The key goes from the key store to the wire only; no record holds it."""
        if not self._paid_ok():
            raise ApiError(409, "paid_calls_disabled", PAID_OFF)
        s = body["backends"]["llm"]
        prof = self.llm.profiles.bind(s["provider_profile_id"], s["model"], s["model_family"])
        key = self.llm.profiles.keys.get(prof.id)
        if key is None:
            raise ApiError(422, "profile_key_missing", f"Provider profile {prof.id} has no key.")
        transport = self.llm.transport() if self.llm.transport else None
        return Endpoint(prof, key, transport=transport, session=f"{eid}:{body['view']}")

    def _call(self, eid: str, p: PlannedCall, r: Rendered, item_set: ItemSet, run_row: dict, endpoint: Endpoint | None) -> dict | None:
        """Judge one planned call and append its records. An LLM call appends a charged `sent` row before the request, so a
        crash after the request leaves a spent call that is never sent again (CR-33 c)."""
        entry = {
            "evaluation_id": eid,
            "run_id": p.run_id,
            "episode_hash": run_row["episode_hash"],
            "block": p.block,
            "view": p.view,
            "view_version": r.view_version,
            "transform": p.transform,
            "input_hash": r.input_hash,
            "backend": p.backend,
            "paraphrase": p.paraphrase,
            "paraphrase_hash": item_set.paraphrase_hash("jev" if p.backend == "jev" else "llm", p.paraphrase - 1),
            "profile_id": None,
            "model": p.backend,
            "returned_version": None,
            "echo_removed": r.echo_removed,
            "cache_hit": False,
            "charged": False,
            "retry_of": None,
            "exploratory": False,
            "input_tokens": 0,
            "output_tokens": 0,
            "error_reason": None,
            "output": None,
        }
        if r.lines and p.backend == "llm":
            return self._ask_llm(eid, p, r, item_set, entry, endpoint)
        requested = _now()
        start = time.perf_counter()
        if not r.lines:
            status, output = "no_talk", None
        elif p.backend == "scripted":
            out = scripted.judge(r.lines, with_commitments=p.view == "talk_prices")
            checked = check(out, r.lines, item_set.quote_min_words, commitments_allowed=p.view == "talk_prices")
            status = checked.status
            output = out.model_dump() | {
                "quotes": [q.model_dump() for q in checked.quotes],
                "commitments": [c.model_dump() for c in checked.commitments],
                "dropped": checked.dropped,
                "quote_lines": list(checked.quote_lines),
                "agreement": checked.agreement,
                "agreement_forced": checked.agreement_forced,
            }
            entry["error_reason"] = checked.reason
        else:
            raise RuntimeError(f"judge backend {p.backend} is not built")
        latency = int((time.perf_counter() - start) * 1000)
        return self._append(eid, p, entry | {"status": status, "output": output}, requested, latency)

    def _append(self, eid: str, p: PlannedCall, entry: dict, requested: str, latency_ms: int) -> dict:
        return append_call(self.writer, eid, p.idx, entry | {"requested_at": requested, "finished_at": _now(), "latency_ms": latency_ms})

    def _ask_llm(self, eid: str, p: PlannedCall, r: Rendered, item_set: ItemSet, entry: dict, endpoint: Endpoint) -> dict:
        """One call, and one retry on an invalid output if the retry reserve has room. Returns the last result record."""
        got = self._llm_attempt(eid, p, r, item_set, entry, endpoint, 0)
        for attempt in range(1, llm_judge.ATTEMPTS):
            if not llm_judge.retry(got["status"]) or not self._room(p, retry=True):
                break
            got = self._llm_attempt(eid, p, r, item_set, entry | {"retry_of": got["line"]}, endpoint, attempt)
        return got

    def _llm_attempt(self, eid: str, p: PlannedCall, r: Rendered, item_set: ItemSet, entry: dict, endpoint: Endpoint, attempt: int) -> dict:
        prof = endpoint.profile
        req = llm_judge.request(item_set, p.paraphrase, r)
        canonical, key = llm_judge.cache_key(req, prof, entry["paraphrase_hash"], entry["input_hash"], attempt)
        entry = entry | {"profile_id": prof.id, "model": prof.model}
        requested = _now()
        if BLOCKS[p.block].cache and (hit := self.llm.cache.get(key)) is not None:  # stability repeats the call (CR-36 m3)
            return self._llm_result(eid, p, r, item_set, entry | {"cache_hit": True}, hit.reply, requested, 0)
        self._chain_intact()  # before each paid request (CR-36 m4)
        if entry["retry_of"] is None and not self._room(p, retry=False):
            raise ApiError(409, "evaluation_budget_exceeded", f"The {p.backend} ledger has no room in block {p.block}.")
        sent = entry | {"status": "sent", "charged": True}
        append_call(self.writer, eid, p.idx, sent | {"requested_at": requested, "finished_at": requested, "latency_ms": 0})
        start = time.perf_counter()
        try:
            reply, latency = endpoint.request(req)
        except EndpointError as e:
            got = self._append(
                eid, p, entry | {"status": "error", "error_reason": e.code}, requested, int((time.perf_counter() - start) * 1000)
            )
            if e.fatal:
                raise ApiError(
                    409, "conflict", f"The LLM judge endpoint answered {e.code}. The job stops; resume it after the fix."
                ) from None
            return got
        self.llm.cache.put(key, canonical, reply, prof.model, prof.host, prof.cost(reply.input_tokens or 0, reply.output_tokens or 0), eid)
        tokens = {"input_tokens": reply.input_tokens or 0, "output_tokens": reply.output_tokens or 0}
        return self._llm_result(eid, p, r, item_set, entry | tokens, reply, requested, latency)

    def _llm_result(
        self, eid: str, p: PlannedCall, r: Rendered, item_set: ItemSet, entry: dict, reply: Reply, requested: str, latency: int
    ) -> dict:
        return self._append(eid, p, entry | llm_judge.result(reply.text, r.lines, item_set, p.view), requested, latency)

    def _room(self, p: PlannedCall, retry: bool) -> bool:
        """Checked again before each paid request (CR-33 m2): a first call needs room in its block and in the backend cap
        less the reserve; a retry needs room in the reserve (m3)."""
        if not BLOCKS[p.block].in_plan:  # its calls were capped per job at create; no retry (the reserve is the plan's)
            return not retry
        state = self.eval_db.ledger_state()
        if retry:
            return state.retries.get(p.backend, 0) < batch.RETRY_RESERVE
        used = sum(n for (b, k), n in state.first.items() if b == p.backend and BLOCKS[k].in_plan)
        in_block = state.first.get((p.backend, p.block), 0)
        return in_block < BLOCKS[p.block].cap_per_backend and used < batch.CAP_PER_BACKEND - batch.RETRY_RESERVE

    def ledger(self) -> dict:
        """GET /judge/ledger: per ledger backend, used (charged first calls), retries_used (from the reserve) and held
        (planned calls of holding jobs), per block, and the state of the hash chain (CR-33 m1)."""
        state = self.eval_db.ledger_state()
        ok, bad = self.eval_db.verify()
        backends = {}
        for b in batch.LEDGER_BACKENDS:
            by_block = {
                k: {
                    "used": state.first.get((b, k), 0),
                    "held": sum(1 for p in state.held if p.backend == b and p.block == k),
                    "cap": blk.cap_per_backend,
                    "in_plan": blk.in_plan,
                }
                for k, blk in BLOCKS.items()
            }
            backends[b] = {
                "used": sum(v["used"] for v in by_block.values() if v["in_plan"]),
                "retries_used": state.retries.get(b, 0),
                "held": sum(v["held"] for v in by_block.values() if v["in_plan"]),
                "by_block": by_block,
            }
        return {
            "cap_per_backend": batch.CAP_PER_BACKEND,
            "retry_reserve": batch.RETRY_RESERVE,
            "chain": {"ok": ok, "first_bad_line": bad},
            "backends": backends,
        }

    def wait(self, eid: str, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while (d := self.detail(eid))["status"] in ("queued", "running"):
            if time.monotonic() > deadline:
                raise TimeoutError(eid)
            time.sleep(0.05)
        return d

    # Read

    def summary(self, row: dict) -> dict:
        body = json.loads(row["body_json"])
        plan = self.eval_db.plan(row["id"])
        calls = self.eval_db.calls(row["id"])
        per_backend = {}
        for b in body["backends"]:
            mine = [c for c in calls if c["backend"] == b]
            per_backend[b] = {
                "calls_planned": sum(1 for p in plan if p.backend == b),
                "calls_done": len({c["plan_idx"] for c in mine}),
                "charged": sum(1 for c in mine if c["charged"]),
                "tokens": sum(c["input_tokens"] + c["output_tokens"] for c in mine),
                "wall_ms": sum(c["latency_ms"] for c in mine if not c["cache_hit"]),
                "counts": dict(Counter(c["status"] for c in mine)),
                "exploratory": sum(1 for c in mine if c["exploratory"]),
            }
        return {
            "id": row["id"],
            "status": row["status"],
            "block": body["block"],
            "view": body["view"],
            "item_set": {"id": body["item_set_id"], "hash": row["item_set_hash"]},
            "backends": body["backends"],
            "order_seed": row["order_seed"],
            "run_ids": sorted({p.run_id for p in plan}),
            "skipped": self._skipped(body, plan),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "per_backend": per_backend,
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    def list(self, run_id: str | None = None, block: str | None = None, status: str | None = None) -> list[dict]:
        out = [self.summary(r) for r in self.eval_db.evaluations()]
        return [
            s
            for s in out
            if (run_id is None or run_id in s["run_ids"])
            and (block is None or s["block"] == block)
            and (status is None or s["status"] == status)
        ]

    def detail(self, eid: str) -> dict:
        row = self.eval_db.evaluation(eid)
        if row is None:
            raise ApiError(404, "not_found", f"Evaluation {eid} not found.")
        out = self.summary(row)
        calls = self.eval_db.calls(eid)
        plan = self.eval_db.plan(eid)
        episodes = [self._episode_body(rid, [c for c in calls if c["run_id"] == rid], plan) for rid in out["run_ids"]]
        out["episodes"] = episodes
        out["agreement"] = _between(episodes)
        out["timing"] = {b: _timing([c for c in calls if c["backend"] == b]) for b in out["backends"]}
        return out

    def _episode_body(self, run_id: str, calls: list[dict], plan: list[PlannedCall]) -> dict:
        row = self.db.run(run_id)
        header = EpisodeHeader.model_validate_json(row["header_json"])
        group = None
        if header.condition_id is not None:
            try:
                group = self.research.condition(header.condition_id).file.group
            except ApiError:
                group = None
        out = {
            "run_id": run_id,
            "condition_id": header.condition_id,
            "group": group,
            "labels": header.labels.model_dump() if header.labels else None,
            "split": header.split,
            "seed": header.seed,
            "input_hash": calls[0]["input_hash"] if calls else None,
            "llm": None,
            "jev": None,
            "scripted": None,
            "calls": [_call_body(c) for c in calls],
        }
        paraphrases = sorted({p.paraphrase for p in plan if p.run_id == run_id})
        for b in ("llm", "scripted"):
            if not any(p.backend == b and p.run_id == run_id for p in plan):
                continue
            mine = [c for c in calls if c["backend"] == b]
            out[b] = {"status": _status(list({c["paraphrase"]: c for c in mine}.values()))} | scoring.backend_scores(mine, paraphrases)
        return out


def _status(calls: list[dict]) -> str | None:
    if not calls:
        return None
    if any(c["status"] == "ok" for c in calls):
        return "ok"
    return Counter(c["status"] for c in calls).most_common(1)[0][0]


def _call_body(c: dict) -> dict:
    keys = (
        "backend",
        "paraphrase",
        "requested_at",
        "finished_at",
        "latency_ms",
        "cache_hit",
        "charged",
        "input_tokens",
        "output_tokens",
        "status",
        "model",
        "returned_version",
        "retry_of",
        "error_reason",
        "echo_removed",
        "exploratory",
    )
    return {k: c[k] for k in keys}


def _paid(row: dict) -> bool:
    return bool({"llm", "jev"} & set(json.loads(row["body_json"])["backends"]))


def _timing(calls: list[dict]) -> dict:
    calls = [c for c in calls if c["status"] != "sent"]
    lat = sorted(c["latency_ms"] for c in calls if not c["cache_hit"] and c["status"] != "no_talk")
    p95 = lat[min(len(lat) - 1, max(0, int(round(0.95 * len(lat))) - 1))] if lat else None
    return {
        "latency_ms": {"median": statistics.median(lat) if lat else None, "p95": p95},
        "wall_ms": sum(lat),
        "calls": len(calls),
        "tokens": sum(c["input_tokens"] + c["output_tokens"] for c in calls),
    }


def _between(episodes: list[dict]) -> dict | None:
    pairs = [
        (e["llm"]["agreement"], e["jev"]["p_agreement_mean"])
        for e in episodes
        if e["llm"] and e["jev"] and e["llm"]["agreement"] is not None and e["jev"].get("p_agreement_mean") is not None
    ]
    if not any(e["llm"] and e["jev"] for e in episodes):
        return None
    same = sum(1 for a, p in pairs if a == int(p >= 0.5))
    return {"n_both_ok": len(pairs), "value": same / len(pairs) if pairs else None, "rule": AGREEMENT_RULE}


def _judged(p: PlannedCall, owner: str) -> ApiError:
    return ApiError(
        409,
        "test_seed_already_run",
        f"Test run {p.run_id} is already judged ({p.view}, {p.transform}, {p.backend}, paraphrase {p.paraphrase}) by evaluation "
        f"{owner}. A test episode is judged once.",
    )
