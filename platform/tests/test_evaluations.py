"""Item 3 unit J2: judge jobs with the scripted judge (plan order, caps, test once, refusals, restart). No model call.
The service is built here from the app's parts; the routes come with the J2 app wiring."""

import json
import shutil
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from adapters.store import Writer
from api.app import create_app
from judge import batch
from judge.blocks import BLOCKS
from judge.store import EvalDB, LedgerState
from services.configs import ApiError
from services.evaluations import EvaluationCreate, EvaluationService
from services.runner import new_id
from test_freeze import TEST_SEED, rule_file
from test_judge import RUBRIC_TEXT, _talkers, write_models
from test_research import ROOT, research_config, started
from condcopy import draft_conditions


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    return tmp_path


@pytest.fixture
def client(root):
    with TestClient(create_app(root, use_dbos=False)) as c:
        yield c


def service(client, root) -> EvaluationService:
    s = client.app.state
    eval_db = EvalDB(root / "data" / "eval.sqlite")
    svc = EvaluationService(s.db, eval_db, Writer(eval_db), s.research, s.judge, new_id, s.evaluations.llm)
    return svc


@pytest.fixture
def evals(client, root):
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    write_models(root / "conditions")
    svc = service(client, root)
    yield svc
    svc.close()
    svc.writer.close()


def body(run_ids, **kw) -> EvaluationCreate:
    return EvaluationCreate.model_validate(
        {"run_ids": run_ids, "block": "wording", "view": "talk_only", "item_set_id": "items-test", "backends": {"scripted": {}}} | kw
    )


def pilot_runs(client, seeds=(1, 2)) -> list[str]:
    return started(client, research_config("C1", seeds=seeds, policies=_talkers("C1")))


def code(fn) -> tuple[int, str]:
    with pytest.raises(ApiError) as e:
        fn()
    return e.value.status, e.value.code


def test_a_scripted_job_judges_every_planned_call_and_aggregates_per_episode(client, evals):
    rids = pilot_runs(client)
    done = evals.wait(evals.create(body(rids))["id"])
    assert done["status"] == "completed" and done["per_backend"]["scripted"]["calls_planned"] == 6
    assert done["per_backend"]["scripted"]["counts"] == {"ok": 6} and done["per_backend"]["scripted"]["charged"] == 0
    for e in done["episodes"]:
        s = e["scripted"]
        assert (e["group"], e["split"], s["n_ok"], s["votes"], s["agreement"]) == ("C1", "pilot", 3, [1, 1, 1], 1)
        assert s["talk_score"] == 1.0 and {q["role"] for q in s["quotes"]} == {"offer", "acceptance"}
        assert all(q["line"] >= 1 for q in s["quotes"]) and len(e["calls"]) == 3
    assert evals.eval_db.verify() == (True, None)
    assert done["agreement"] is None and done["timing"]["scripted"]["calls"] == 6
    ledger = evals.ledger()  # the scripted judge makes no request, so the ledger is untouched
    assert ledger["cap_per_backend"] == 280 and all(b["used"] == b["held"] == 0 for b in ledger["backends"].values())


def test_the_call_order_is_interleaved_and_fixed_by_the_order_seed():
    episodes = [("run_a", "dev"), ("run_b", "dev")]
    one = batch.plan(episodes, "main", "talk_only", ["llm", "jev"], 7)
    assert one == batch.plan(list(reversed(episodes)), "main", "talk_only", ["jev", "llm"], 7)
    assert one != batch.plan(episodes, "main", "talk_only", ["llm", "jev"], 8)
    assert len(one) == 12 and [p.idx for p in one] == list(range(12))
    backends = [p.backend for p in one]
    assert backends != sorted(backends) and backends != sorted(backends, reverse=True)


def test_an_episode_with_no_talk_is_recorded_as_no_talk_with_no_call(client, evals):
    [rid] = started(client, research_config("C2a", policies=_talkers("C2a")))
    done = evals.wait(evals.create(body([rid]))["id"])
    assert done["per_backend"]["scripted"]["counts"] == {"no_talk": 3}
    assert done["episodes"][0]["scripted"]["status"] == "no_talk" and done["episodes"][0]["scripted"]["n_ok"] == 0


def test_branch_children_and_unfinished_runs_are_excluded_and_refused(client, evals):
    [rid] = pilot_runs(client, seeds=(1,))
    child = client.post(f"/api/runs/{rid}/branches", json={"at": 3, "rounds": 2, "n": 1, "intervention": {"type": "placebo"}})
    child = child.json()["run_ids"][0]
    client.app.state.svc.wait(child, timeout=30)
    est = evals.estimate(body([rid, child, "run_nope"]))
    assert est["excluded"] == [
        {"run_id": child, "index": 1, "reason": "not_parent"},
        {"run_id": "run_nope", "index": 2, "reason": "not_found"},
    ]
    assert est["episodes"] == 1 and est["per_backend"]["scripted"]["calls"] == 3
    with pytest.raises(ApiError) as e:
        evals.create(body([rid, child]))
    assert (e.value.status, e.value.code, [x["path"] for x in e.value.errors]) == (422, "validation_error", ["run_ids/1"])
    assert evals.eval_db.evaluations() == []


def test_a_block_judges_only_its_splits_and_views(client, evals):
    [rid] = pilot_runs(client, seeds=(1,))
    est = evals.estimate(body([rid], block="main"))
    assert est["excluded"] == [{"run_id": rid, "index": 0, "reason": "split_not_in_block"}] and est["episodes"] == 0
    with pytest.raises(ApiError) as e:
        evals.create(body([rid], block="main"))
    assert (e.value.code, e.value.errors[0]["path"]) == ("validation_error", "run_ids/0")
    with pytest.raises(ApiError) as e:
        evals.estimate(body([rid], block="main", view="talk_prices"))
    assert (e.value.code, e.value.errors[0]["path"]) == ("validation_error", "view")


def test_dev_episodes_need_a_valid_conditions_stage_and_the_rubric_frozen_in_it(client, root, evals):
    rubric = root / "conditions" / "judge-rubric.yaml"
    rubric.write_text(RUBRIC_TEXT.replace("quote_min_words: ", "quote_min_words: 1"), encoding="utf-8")
    client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"})
    [rid] = started(client, research_config("C1", split="dev", seeds=(101,), policies=_talkers("C1")))
    rubric.write_text(RUBRIC_TEXT, encoding="utf-8")  # a rubric change after the stage
    assert code(lambda: evals.create(body([rid], block="main"))) == (409, "condition_not_frozen")
    client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"})
    assert evals.wait(evals.create(body([rid], block="main"))["id"])["status"] == "completed"


def test_a_block_judges_only_its_conditions(client, root, evals):
    """DECISIONS-13 §7: main judges C1, C2a and C3-accept; C3-silent goes to support_one_wording."""
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"})
    [silent] = started(client, research_config("C3-silent", split="dev", seeds=(101,), policies=_talkers("C3-silent")))
    [c1] = started(client, research_config("C1", split="dev", seeds=(101,), policies=_talkers("C1")))
    est = evals.estimate(body([silent, c1], block="main"))
    assert est["excluded"] == [{"run_id": silent, "index": 0, "reason": "condition_not_in_block"}] and est["episodes"] == 1
    est = evals.estimate(body([silent, c1], block="support_one_wording"))
    assert est["excluded"] == [{"run_id": c1, "index": 1, "reason": "condition_not_in_block"}] and est["episodes"] == 1
    assert est["per_backend"]["scripted"]["calls"] == 1  # one wording
    assert code(lambda: evals.create(body([silent], block="main"))) == (422, "validation_error")


def freeze_both(client, root) -> None:
    """Both freeze stages with the test item set as the rubric."""
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    write_models(root / "conditions")
    rule_file(root)
    for stage in ("conditions", "rule"):
        assert client.post("/api/freeze", json={"stage": stage, "approved_by": "lead"}).status_code == 200


def a_test_split_run(client) -> str:
    [rid] = started(client, research_config("C1", split="test", seeds=(TEST_SEED,), policies=_talkers("C1")))
    return rid


def test_a_test_episode_is_judged_once(client, root, evals):
    freeze_both(client, root)
    rid = a_test_split_run(client)
    first = evals.wait(evals.create(body([rid], block="main"))["id"])
    assert first["status"] == "completed"
    for fn in (evals.estimate, evals.create):
        with pytest.raises(ApiError) as e:
            fn(body([rid], block="main"))
        assert (e.value.status, e.value.code) == (409, "test_seed_already_run") and first["id"] in e.value.message
    assert evals.eval_db.judgement((rid, "talk_only", "none", "scripted", 1)) == first["id"]


def test_two_parallel_test_jobs_store_one(client, root, evals, monkeypatch):
    freeze_both(client, root)
    rid = a_test_split_run(client)
    real, barrier = evals.plan, threading.Barrier(2)

    def plan(b, eid):
        out = real(b, eid)
        barrier.wait(timeout=10)  # both pass every read check before either stores
        return out

    monkeypatch.setattr(evals, "plan", plan)
    results = []

    def go():
        try:
            results.append(evals.create(body([rid], block="main"))["id"])
        except ApiError as e:
            results.append(e.code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r == "test_seed_already_run" for r in results) == [False, True]
    assert len(evals.eval_db.evaluations()) == 1


def test_the_test_lock_rows_and_call_records_cannot_change(client, root, evals):
    freeze_both(client, root)
    rid = a_test_split_run(client)
    evals.wait(evals.create(body([rid], block="main"))["id"])
    for sql, match in (
        ("DELETE FROM judge_cells", "made once"),
        ("UPDATE judge_cells SET evaluation_id = 'x'", "made once"),
        ("DELETE FROM judge_calls", "append-only"),
        ("UPDATE judge_calls SET entry_json = '{}'", "append-only"),
    ):
        with pytest.raises(Exception, match=match):
            evals.writer.submit(lambda conn, sql=sql: conn.execute(sql)).result()


def test_paid_backends_meet_the_paid_switch_then_the_family_check(client, evals, monkeypatch):
    [rid] = pilot_runs(client, seeds=(1,))
    llm = {"llm": {"provider_profile_id": "prof_x", "model": "m", "model_family": "fam"}}
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    assert code(lambda: evals.estimate(body([rid], backends=llm))) == (409, "paid_calls_disabled")
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")
    monkeypatch.setattr(evals, "_families", lambda run_id: {"fam"})
    assert code(lambda: evals.estimate(body([rid], backends=llm))) == (422, "judge_same_family")
    monkeypatch.setattr(evals, "_families", lambda run_id: {"jev"})
    jev = {"jev": {"provider_profile_id": "prof_j", "model": "jev-1.13"}}
    assert code(lambda: evals.estimate(body([rid], backends=jev))) == (422, "judge_same_family")
    monkeypatch.setattr(evals, "_families", lambda run_id: set())
    assert code(lambda: evals.estimate(body([rid], backends=llm))) == (404, "not_found")  # llm is built: its profile is read
    assert code(lambda: evals.estimate(body([rid], backends=jev))) == (422, "protocol_not_built")
    with pytest.raises(ValueError):
        body([rid], backends={"jev": {"provider_profile_id": "p", "model": "jev-latest"}})


def test_the_ledger_caps_count_first_calls_and_held_calls_per_block():
    held = batch.plan([("run_h", "pilot")], "wording", "talk_only", ["llm"], 1)  # 3 held by an open job
    new = batch.plan([("run_n", "pilot")], "wording", "talk_only", ["llm", "jev"], 1)
    rows = batch.room(LedgerState({("llm", "wording"): 15}, {}, held), new)
    llm = next(r for r in rows if r["backend"] == "llm")
    assert (llm["used"], llm["held"], llm["calls"], llm["block_cap"]) == (15, 3, 3, 20)
    assert "llm block wording" in batch.over_cap(rows)
    assert batch.over_cap(batch.room(LedgerState({("llm", "wording"): 14}, {}, held), new)) is None


def test_a_retry_is_paid_from_the_reserve_and_takes_no_block_room():
    main = batch.plan([("run_n", "dev")], "main", "talk_only", ["llm"], 1)
    first = {("llm", k): b.cap_per_backend for k, b in BLOCKS.items()} | {("llm", "main"): BLOCKS["main"].cap_per_backend - len(main)}
    [row] = batch.room(LedgerState(first, {"llm": 5}, []), main)
    assert (row["retries_used"], row["block_used"] + row["calls"]) == (5, BLOCKS["main"].cap_per_backend)
    assert batch.over_cap([row]) is None
    assert "llm block main" in batch.over_cap(batch.room(LedgerState(first | {("llm", "main"): first[("llm", "main")] + 1}, {}, []), main))


class Died(BaseException):
    """The server process ends between two calls."""


def test_a_job_resumed_after_a_restart_makes_no_call_twice(client, root, evals, monkeypatch):
    rids = pilot_runs(client)
    evals.close()  # no worker: the test runs the job itself
    real, count = evals._call, {"n": 0}

    def die_at_the_third(*args):
        count["n"] += 1
        if count["n"] == 3:
            raise Died
        return real(*args)

    monkeypatch.setattr(evals, "_call", die_at_the_third)
    eid = evals.create(body(rids))["id"]
    with pytest.raises(Died):
        evals._run(eid)
    assert len(evals.eval_db.calls(eid)) == 2 and evals.eval_db.evaluation(eid)["status"] == "running"
    monkeypatch.undo()
    again = service(client, root)
    before = {c["plan_idx"] for c in again.eval_db.calls(eid)}
    again.recover()
    done = again.wait(eid)
    idx = [c["plan_idx"] for c in again.eval_db.calls(eid)]
    assert done["status"] == "completed" and sorted(idx) == list(range(6)) and len(idx) == len(set(idx))
    assert before <= set(idx)
    again.close()
    again.writer.close()


# Routes (contract 0.15.0)


def test_the_routes_follow_the_contract_and_the_ledger_shows_no_scripted_call(client, root):
    from test_api import check as contract

    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    write_models(root / "conditions")
    rids = pilot_runs(client)
    req = {"run_ids": rids, "block": "wording", "view": "talk_only", "item_set_id": "items-test", "backends": {"scripted": {}}}
    est = contract(client.post("/api/evaluations/estimate", json=req), "/evaluations/estimate", "post")
    assert (est["episodes"], est["excluded"], est["per_backend"]["scripted"]["calls"]) == (2, [], 6)
    job = contract(client.post("/api/evaluations", json=req), "/evaluations", "post")
    done = client.app.state.evaluations.wait(job["id"])
    got = contract(client.get(f"/api/evaluations/{job['id']}"), "/evaluations/{evaluation_id}")
    assert got["status"] == done["status"] == "completed" and len(got["episodes"]) == 2
    listed = contract(client.get("/api/evaluations", params={"run_id": rids[0]}), "/evaluations")
    assert [e["id"] for e in listed] == [job["id"]] and "episodes" not in listed[0]
    assert contract(client.get("/api/evaluations", params={"status": "failed"}), "/evaluations") == []
    ledger = contract(client.get("/api/judge/ledger"), "/judge/ledger")
    assert ledger["cap_per_backend"] == 280 and ledger["backends"]["llm"]["used"] == 0 and ledger["chain"]["ok"] is True
    again = contract(client.post(f"/api/evaluations/{job['id']}/resume"), "/evaluations/{evaluation_id}/resume", "post")
    assert (again["status"], again["code"]) == (409, "conflict")
    assert contract(client.get("/api/evaluations/eval_nope"), "/evaluations/{evaluation_id}")["code"] == "not_found"


def test_the_routes_refuse_a_bad_body_and_a_branch_child_with_its_index(client, root):
    from test_api import check as contract

    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    write_models(root / "conditions")
    [rid] = pilot_runs(client, seeds=(1,))
    child = client.post(f"/api/runs/{rid}/branches", json={"at": 3, "rounds": 2, "n": 1, "intervention": {"type": "placebo"}})
    child = child.json()["run_ids"][0]
    client.app.state.svc.wait(child, timeout=30)
    req = {"run_ids": [rid, child], "block": "wording", "view": "talk_only", "item_set_id": "items-test", "backends": {"scripted": {}}}
    r = contract(client.post("/api/evaluations", json=req), "/evaluations", "post")
    assert (r["status"], r["code"], [e["path"] for e in r["errors"]]) == (422, "validation_error", ["run_ids/1"])
    both = req | {"experiment_id": "exp_x"}
    assert client.post("/api/evaluations", json=both).status_code == 422
    assert client.post("/api/evaluations", json=req | {"backends": {}}).status_code == 422
    assert client.app.state.evaluations.eval_db.evaluations() == []


# CR-33 fixes


def paid_llm(monkeypatch, evals):
    """An llm backend past the paid switch; the profile is unknown, so these tests stop before the profile check."""
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")
    evals.close()
    return {"llm": {"provider_profile_id": "prof_x", "model": "m", "model_family": "fam"}}


def test_a_broken_judge_chain_shows_in_the_ledger_and_refuses_paid_calls(client, evals, monkeypatch):
    rids = pilot_runs(client, seeds=(1,))
    evals.wait(evals.create(body(rids))["id"])
    assert evals.ledger()["chain"] == {"ok": True, "first_bad_line": None}
    conn = sqlite3.connect(evals.eval_db.path)
    conn.execute("DROP TRIGGER judge_calls_no_update")
    conn.execute("UPDATE judge_calls SET entry_json = replace(entry_json, '\"charged\":false', '\"charged\":true') WHERE line = 2")
    conn.commit()
    conn.close()
    assert evals.ledger()["chain"] == {"ok": False, "first_bad_line": 2}
    llm = paid_llm(monkeypatch, evals)
    assert code(lambda: evals.estimate(body(rids, backends=llm))) == (409, "conflict")


def test_an_llm_seller_with_no_model_family_refuses_the_llm_and_jev_judges(client, evals, monkeypatch):
    [rid] = pilot_runs(client, seeds=(1,))
    real = evals.db.run_config(rid)
    cfg = json.loads(real["config_json"])
    cfg["agents"][0]["policy"] = {"kind": "llm", "llm": {"provider_profile_id": "prof_s", "model": "m-small"}}
    monkeypatch.setattr(evals.db, "run_config", lambda run_id: dict(real) | {"config_json": json.dumps(cfg)})
    llm = paid_llm(monkeypatch, evals)
    jev = {"jev": {"provider_profile_id": "prof_j", "model": "jev-1.13"}}
    for backends in (llm, jev):
        with pytest.raises(ApiError) as e:
            evals.estimate(body([rid], backends=backends))
        assert (e.value.status, e.value.code) == (422, "judge_same_family") and "no model_family" in e.value.message


def test_a_job_fails_when_the_rubric_changed_after_its_plan_and_resumes_only_on_the_planned_rubric(client, root, evals):
    rids = pilot_runs(client, seeds=(1,))
    evals.close()
    eid = evals.create(body(rids))["id"]
    rubric = root / "conditions" / "judge-rubric.yaml"
    rubric.write_text(RUBRIC_TEXT + "\n# edited after the plan\n", encoding="utf-8")
    evals._run_safely(eid)
    row = evals.detail(eid)
    assert (row["status"], row["error"]["code"], evals.eval_db.calls(eid)) == ("failed", "hash_mismatch", [])
    assert code(lambda: evals.resume(eid)) == (409, "hash_mismatch")
    rubric.write_text(RUBRIC_TEXT, encoding="utf-8")
    again = service(client, root)
    assert again.resume(eid)["id"] == eid
    assert again.wait(eid)["status"] == "completed"
    again.close()
    again.writer.close()


def test_a_failed_test_job_resumes_under_its_id_and_keeps_its_cells(client, root, evals, monkeypatch):
    freeze_both(client, root)
    rid = a_test_split_run(client)
    evals.close()
    real, count = evals._call, {"n": 0}

    def fail_at_the_second(*args):
        count["n"] += 1
        if count["n"] == 2:
            raise RuntimeError("provider down")
        return real(*args)

    monkeypatch.setattr(evals, "_call", fail_at_the_second)
    eid = evals.create(body([rid], block="main"))["id"]
    evals._run_safely(eid)
    assert evals.detail(eid)["status"] == "failed" and len(evals.eval_db.calls(eid)) == 1
    with pytest.raises(ApiError) as e:
        evals.create(body([rid], block="main"))
    assert e.value.code == "test_seed_already_run" and eid in e.value.message
    monkeypatch.undo()
    again = service(client, root)
    assert again.resume(eid)["id"] == eid
    done = again.wait(eid)
    idx = [c["plan_idx"] for c in again.eval_db.calls(eid)]
    assert done["status"] == "completed" and sorted(idx) == list(range(len(again.eval_db.plan(eid))))
    assert again.eval_db.judgement((rid, "talk_only", "none", "scripted", 1)) == eid
    assert len(again.eval_db.evaluations()) == 1
    assert code(lambda: again.resume(eid)) == (409, "conflict")
    again.close()
    again.writer.close()


def test_an_experiment_request_takes_the_block_conditions_and_skips_the_rest(client, root, evals):
    """CR-64 finding 1: a four-condition experiment; each block judges its own conditions and lists the others as skipped."""
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"})
    conditions = ["C1", "C2a", "C3-accept", "C3-silent"]

    def agents(cid):
        return [{"id": s, "role": "seller", "policy": {"kind": k, "params": p}} for s, (k, p) in _talkers(cid).items()]

    req = {"conditions": conditions, "split": "dev", "seeds": [101], "agents": agents("C1"),
           "agents_by_condition": {c: agents(c) for c in conditions}, "budget": {"max_usd": 1.0},
           "batch_budget": {"max_calls": 1000}}  # fmt: skip
    exp = client.post("/api/experiments", json=req)
    assert exp.status_code < 300, exp.text
    exp_id = exp.json()["id"]
    client.app.state.experiments.wait(exp_id, timeout=120)
    condition_of = {r["run_id"]: r["condition_id"] for r in client.app.state.db.experiment_runs(exp_id) if r["kind"] == "parent"}
    silent = next(r for r, c in condition_of.items() if c == "C3-silent")
    est = evals.estimate(body(None, experiment_id=exp_id, block="main"))
    assert (est["episodes"], est["excluded"]) == (3, [])
    assert est["skipped"] == [{"run_id": silent, "reason": "condition_not_in_block"}]
    job = evals.wait(evals.create(body(None, experiment_id=exp_id, block="support_one_wording"))["id"])
    assert job["status"] == "completed" and job["run_ids"] == [silent]
    assert sorted(s["run_id"] for s in job["skipped"]) == sorted(r for r in condition_of if r != silent)
    assert {s["reason"] for s in job["skipped"]} == {"condition_not_in_block"}
