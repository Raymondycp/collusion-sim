"""Research unit 2: experiments (DECISIONS-10 §3). Scripted sellers, and the fake endpoint for the batch budget; no paid call."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from adapters.llm.policy import ATTEMPTS
from api.app import create_app
from fakellm import FakeLLM, cooperator
from services.catalog import Catalog, Paths
from services.research import Research
from simschema.events import RunLimits
from test_api import SPEC, check
from test_profiles import KEY, MemoryKeyring, no_key_anywhere, profile_body
from test_research import ROOT
from condcopy import draft_conditions

MAX_STEPS = RunLimits().max_steps_per_turn

SCRIPTED = [
    {"id": "A", "role": "seller", "policy": {"kind": "grim_trigger", "params": {"target": 10.5}}},
    {"id": "B", "role": "seller", "policy": {"kind": "constant", "params": {"price": 10.5}}},
]
SAME = [{"id": s, "role": "seller", "policy": {"kind": "constant", "params": {"price": 5.75}}} for s in "AB"]


def body(conditions=("C1",), seeds=(1,), **kw):
    return {
        "conditions": list(conditions),
        "split": "pilot",
        "seeds": list(seeds),
        "agents": SCRIPTED,
        "budget": {"max_usd": 1.0},
        "batch_budget": {"max_calls": 1000},
        **kw,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    fake = FakeLLM(cooperator(10.5))
    with TestClient(create_app(tmp_path, use_dbos=False, key_backend=MemoryKeyring(), llm_transport=fake.transport)) as c:
        c.fake = fake
        yield c


def created(client, b) -> dict:
    return check(client.post("/api/experiments", json=b), "/experiments", "post")


def finished(client, exp_id) -> dict:
    client.app.state.experiments.wait(exp_id, timeout=120)
    return check(client.get(f"/api/experiments/{exp_id}"), "/experiments/{experiment_id}")


def run(client, rid) -> dict:
    return client.get(f"/api/runs/{rid}").json()


def stop_round(client, seed) -> int:
    research = client.app.state.research
    entry = research.seed_map().entry(seed)
    return research.catalog.scenario(entry.scenario_id).horizon.stop_round(seed)


def started_events(client, rid) -> int:
    return sum(e.type == "run_started" for e in client.app.state.db.events(rid))


def llm_agents(pid, both=True):
    llm = {"kind": "llm", "llm": {"provider_profile_id": pid, "model": "m-small", "model_family": "fake", "temperature": 0.2}}
    return [{"id": "A", "role": "seller", "policy": llm}, {"id": "B", "role": "seller", "policy": llm if both else SCRIPTED[1]["policy"]}]


# Plan and branches


def test_each_cell_runs_its_parent_then_the_planned_branches_from_the_fork_rounds(client):
    exp = created(client, body(("C1", "C2a"), agents_by_condition={"C2a": SAME}))
    assert exp["status"] == "running" and exp["id"].startswith("exp_")
    done = finished(client, exp["id"])
    assert done["status"] == "completed" and done["counts"] == {"completed": 2}
    cells = {c["condition_id"]: c for c in done["cells"]}
    stop = stop_round(client, 1)
    for cid, cell in cells.items():
        parent = run(client, cell["parent"]["run_id"])
        assert parent["condition_id"] == cid and parent["seed"] == 1 and parent["t"] == stop
        kinds = [b["kind"] for b in cell["branches"]]
        assert kinds.count("probe") == 1 and kinds.count("placebo") == 3
        assert kinds.count("ablation") == 1  # every group; C2a has no channel, so a null ablation
        for b in cell["branches"]:
            child = run(client, b["run_id"])
            br = child["branch"]
            assert b["status"] == "completed" and br["parent_run_id"] == parent["id"]
            assert br["intervention"]["type"] == b["kind"]
            assert child["created_at"] >= parent["ended_at"]
            if b["kind"] == "ablation":  # leaves the condition: dependence evidence, never in a case group
                header = client.get(f"/api/runs/{b['run_id']}/events", params={"limit": 1}).json()["events"][0]["header"]
                assert (child.get("labels"), header["parent_labels"], child["condition_id"]) == (None, parent["labels"], None)
            else:
                assert child["labels"] == parent["labels"] and child["condition_id"] == cid
            if b["kind"] == "ablation":
                assert (br["fork_t"], child["t"]) == (10, stop)
                null = {"type": "ablation", "ablated": "none", "channel_id": None, "mode": None}
                channel = {"type": "ablation", "ablated": "channel", "channel_id": "private_A_B", "mode": "shuffle"}
                assert br["intervention"] == (channel if cid == "C1" else null)
                assert br["sample_index"] == (0 if cid == "C1" else 1000)  # a null ablation draws fresh samples
            else:
                assert (br["fork_t"], child["t"]) == (20, 24)
        placebo = sorted(run(client, b["run_id"])["branch"]["sample_index"] for b in cell["branches"] if b["kind"] == "placebo")
        assert placebo == [1, 2, 3]
    assert run(client, cells["C2a"]["parent"]["run_id"])["config_hash"] != run(client, cells["C1"]["parent"]["run_id"])["config_hash"]


def test_each_run_summary_names_its_experiment_and_a_branch_the_plan_did_not_make_has_null(client):
    exp = finished(client, created(client, body())["id"])
    parent = exp["cells"][0]["parent"]["run_id"]
    planned = {parent, *(b["run_id"] for b in exp["cells"][0]["branches"])}
    req = {"at": stop_round(client, 1) - 4, "rounds": 3, "n": 1, "intervention": {"type": "placebo"}}
    (own,) = check(client.post(f"/api/runs/{parent}/branches", json=req), "/runs/{run_id}/branches", "post")["run_ids"]
    client.app.state.svc.wait(own, timeout=30)
    items = {r["id"]: r["experiment_id"] for r in check(client.get("/api/runs"), "/runs")["items"] if r["id"] in planned | {own}}
    assert items == {**dict.fromkeys(planned, exp["id"]), own: None}
    assert (run(client, parent)["experiment_id"], run(client, own)["experiment_id"]) == (exp["id"], None)


def test_a_branch_plan_part_set_to_null_makes_no_children(client):
    exp = created(client, body(branch_plan={"probe": None, "placebo": {"n": 1}, "ablation": None}))
    (cell,) = finished(client, exp["id"])["cells"]
    assert [b["kind"] for b in cell["branches"]] == ["placebo"]


def test_advancing_again_or_branching_a_stored_child_again_runs_nothing_twice(client):
    exp = finished(client, created(client, body())["id"])
    ids = [exp["cells"][0]["parent"]["run_id"]] + [b["run_id"] for b in exp["cells"][0]["branches"]]
    db, svc = client.app.state.db, client.app.state.svc
    before = len(db.runs())
    client.app.state.experiments.advance(exp["id"])
    client.app.state.experiments.recover()
    from simcore.branch import BranchRequest
    from simschema.events import ProbeIntervention

    probe = next(b["run_id"] for b in exp["cells"][0]["branches"] if b["kind"] == "probe")
    svc.branch(ids[0], BranchRequest(at=20, rounds=5, intervention=ProbeIntervention(target="B")), ids=[probe])
    svc.close()  # waits for anything launched
    assert len(db.runs()) == before
    assert all(started_events(client, rid) == 1 for rid in ids)


# Crash recovery


def test_planned_parents_that_never_started_run_after_a_restart(client, monkeypatch):
    svc, experiments = client.app.state.svc, client.app.state.experiments
    monkeypatch.setattr(svc, "commit", lambda prepared: None)  # the server dies after the experiment rows are written
    exp = created(client, body(branch_plan={"probe": None, "placebo": {"n": 1}, "ablation": None}))
    assert exp["cells"][0]["status"] == "planned" and client.app.state.db.runs() == []
    monkeypatch.undo()
    experiments.recover()
    done = finished(client, exp["id"])
    assert done["status"] == "completed" and [b["status"] for b in done["cells"][0]["branches"]] == ["completed"]
    assert done["cells"][0]["parent"]["run_id"] == exp["cells"][0]["parent"]["run_id"]


def test_branches_of_a_parent_that_completed_before_a_restart_start_at_recovery(client):
    svc, experiments = client.app.state.svc, client.app.state.experiments
    svc.on_finished = lambda run_id: None  # the server dies right after the parent's ledger line
    exp = created(client, body(branch_plan={"probe": {}, "placebo": None, "ablation": None}))
    parent = exp["cells"][0]["parent"]["run_id"]
    svc.wait(parent, timeout=60)
    cell = check(client.get(f"/api/experiments/{exp['id']}"), "/experiments/{experiment_id}")["cells"][0]
    assert cell["status"] == "branching" and cell["branches"][0]["status"] == "planned"
    experiments.recover()
    done = finished(client, exp["id"])
    assert done["status"] == "completed" and started_events(client, parent) == 1


# Estimate


def test_the_estimate_counts_every_planned_run_and_no_calls_for_scripted_sellers(client):
    est = check(
        client.post("/api/experiments/estimate", json=body(("C1", "C2a"), seeds=(1, 2), agents_by_condition={"C2a": SAME})),
        "/experiments/estimate",
        "post",
    )
    assert est["n_cells"] == 4
    assert est["runs"] == {"parents": 4, "probe": 4, "placebo": 12, "ablation": 4, "total": 24}
    assert est["calls_max"] == 0 and est["tokens_max"] == {"input": 0, "output": 0} and est["within_budget"] is True
    assert client.app.state.db.runs() == [] and client.get("/api/experiments").json() == []


def test_the_llm_estimate_is_the_worst_case_over_every_round_of_every_run(client):
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    b = body(agents=llm_agents(pid, both=False), batch_budget={"max_calls": 10})
    est = check(client.post("/api/experiments/estimate", json=b), "/experiments/estimate", "post")
    stop = stop_round(client, 1)
    rounds = stop + 5 + 3 * 5 + (stop - 10 + 1)
    turns = 2  # C1 is talk_and_act and A may post
    # DECISIONS-13 §1: C1 has no queries and one channel, so each turn is one step; only the attempts multiply.
    assert est["calls_max"] == rounds * turns * ATTEMPTS
    assert est["usd_max"] > 0 and est["pricing_missing_for"] == [] and est["within_budget"] is False
    assert KEY not in json.dumps(est)


def test_a_missing_price_gives_a_null_usd_and_names_the_profile(client):
    pid = client.post("/api/provider-profiles", json=profile_body(pricing=None)).json()["id"]
    b = body(agents=llm_agents(pid), batch_budget={"max_usd": 5})
    est = check(client.post("/api/experiments/estimate", json=b), "/experiments/estimate", "post")
    assert est["usd_max"] is None and est["pricing_missing_for"] == [pid] and est["within_budget"] is None
    assert est["calls_max"] > 0


# Batch budget


def test_the_batch_budget_caps_paid_calls_across_all_runs_of_the_experiment(client, monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    cap = 12
    exp = created(client, body(seeds=(1, 2), agents=llm_agents(pid), batch_budget={"max_calls": cap}))
    done = finished(client, exp["id"])
    db = client.app.state.db
    ids = [c["parent"]["run_id"] for c in done["cells"]] + [b["run_id"] for c in done["cells"] for b in c["branches"]]
    calls = db.paid_calls(ids)["calls"]
    assert 0 < calls <= cap and done["spend"]["calls"] == calls
    assert len(client.fake.bodies) <= cap
    assert {c["parent"]["end_reason"] for c in done["cells"]} == {"budget_exceeded"}  # the per-run budget alone would not stop them
    assert done["status"] == "ended" and all(c["branches"] and c["branches"][0]["status"] == "planned" for c in done["cells"])
    experiments = client.app.state.experiments
    experiments._guards.clear()  # a restarted server rebuilds the guard from the stored calls
    assert experiments.guard(ids[0]).calls == calls
    no_key_anywhere(tmp_path, json.dumps(done))


# Refusals: nothing is stored


@pytest.mark.parametrize(
    ("change", "status", "code", "path"),
    [
        ({"split": "test", "seeds": [201]}, 409, "test_split_locked", None),
        ({"split": "dev", "seeds": [101]}, 409, "condition_not_frozen", None),
        ({"seeds": [101]}, 422, "seed_not_in_split", None),
        ({"conditions": ["C9"]}, 422, "validation_error", "conditions/0"),
        ({"branch_plan": {"probe": {"at": 40}}}, 422, "validation_error", "branch_plan/probe"),
        ({"branch_plan": {"probe": {"target": "Z"}}}, 422, "validation_error", "branch_plan/probe/target"),
        ({"conditions": ["C2b"], "agents": SAME}, 422, None, None),
        ({"batch_budget": {}}, 422, "validation_error", None),
        ({"seeds": [1, 1]}, 422, "validation_error", None),
    ],
)
def test_a_refused_experiment_stores_no_experiment_and_no_run(client, change, status, code, path):
    for url, route in (("/api/experiments/estimate", "/experiments/estimate"), ("/api/experiments", "/experiments")):
        resp = check(client.post(url, json={**body(), **change}), route, "post")
        assert (resp["status"], resp["code"] if code else None) == (status, code), resp
        assert resp.get("code") != "paid_calls_disabled"
        if path:
            assert path in [e["path"] for e in resp["errors"]]
    assert client.get("/api/experiments").json() == [] and client.app.state.db.runs() == []


def test_c2b_takes_its_own_agents_with_a_model_family_per_seller(client):
    best = [{"id": "A", "role": "seller", "policy": {"kind": "best_response", "params": {}}}, SCRIPTED[1]]
    b = body(("C2b",), agents_by_condition={"C2b": best})
    assert client.post("/api/experiments/estimate", json=b).status_code == 200


def test_llm_agents_are_refused_while_paid_calls_are_off_but_can_be_estimated(client, monkeypatch):
    monkeypatch.setattr(client.app.state.svc.llm, "transport", None)  # a real network transport, with SIM_ALLOW_PAID unset
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    b = body(agents=llm_agents(pid))
    assert client.post("/api/experiments/estimate", json=b).status_code == 200
    resp = check(client.post("/api/experiments", json=b), "/experiments", "post")
    assert resp["code"] == "paid_calls_disabled" and client.get("/api/experiments").json() == []
    assert client.fake.bodies == []


def test_an_unknown_experiment_is_404(client):
    resp = check(client.get("/api/experiments/exp_01J000000000000000000000ZZ"), "/experiments/{experiment_id}")
    assert resp["code"] == "not_found"


def test_the_list_shows_newest_first(client):
    a = created(client, body(branch_plan={"probe": None, "placebo": None, "ablation": None}))
    b = created(client, body(name="second", branch_plan={"probe": None, "placebo": None, "ablation": None}))
    listed = check(client.get("/api/experiments"), "/experiments")
    assert [e["id"] for e in listed] == [b["id"], a["id"]] and listed[0]["name"] == "second"
    assert a["hash"] == b["hash"]  # the name is not part of the hash
    for e in (a, b):
        finished(client, e["id"])


def test_the_contract_lists_the_experiment_operations():
    assert {"get", "post"} <= set(SPEC["paths"]["/experiments"])
    assert "post" in SPEC["paths"]["/experiments/estimate"] and "get" in SPEC["paths"]["/experiments/{experiment_id}"]


def test_a_stored_run_that_never_launched_runs_after_a_restart(client, monkeypatch):
    svc, experiments = client.app.state.svc, client.app.state.experiments
    monkeypatch.setattr(svc, "_launch", lambda run_id: None)  # config stored, then the server dies before the queue takes it
    exp = created(client, body(branch_plan={"probe": None, "placebo": None, "ablation": None}))
    rid = exp["cells"][0]["parent"]["run_id"]
    assert client.app.state.db.run_config(rid) is not None and exp["cells"][0]["status"] == "queued"
    monkeypatch.undo()
    experiments.recover()
    assert finished(client, exp["id"])["status"] == "completed" and started_events(client, rid) == 1


def test_ablation_children_go_only_to_the_planned_groups(client):
    b = body(("C1", "C3-accept"), branch_plan={"probe": None, "placebo": None, "ablation": {"groups": ["C1"]}})
    est = check(client.post("/api/experiments/estimate", json=b), "/experiments/estimate", "post")
    assert est["runs"]["ablation"] == 1 and [r["runs"] for r in est["per_condition"]] == [2, 1]


def test_a_cell_whose_branch_was_half_stored_before_a_restart_runs_each_child_once(client, monkeypatch):
    svc, experiments = client.app.state.svc, client.app.state.experiments
    svc.on_finished = lambda run_id: None
    exp = created(client, body(branch_plan={"probe": None, "placebo": {"n": 2}, "ablation": None}))
    svc.wait(exp["cells"][0]["parent"]["run_id"], timeout=60)
    monkeypatch.setattr(svc, "_launch", lambda run_id: None)
    experiments.advance(exp["id"])  # both children stored, none launched
    monkeypatch.undo()
    first, second = (b["run_id"] for b in exp["cells"][0]["branches"])
    svc.writer.submit(lambda conn: conn.execute("DELETE FROM run_configs WHERE run_id = ?", (second,))).result()
    svc.on_finished = experiments._finished
    launched, real = [], svc._launch
    monkeypatch.setattr(svc, "_launch", lambda rid: (launched.append(rid), real(rid)))
    experiments.recover()  # launches the stored child, then branches again for the missing one
    done = finished(client, exp["id"])
    assert done["status"] == "completed"
    assert sorted(launched) == sorted([first, second])  # a second launch would run the first child twice at once


def test_advance_never_branches_a_parent_that_did_not_complete(client, monkeypatch):
    svc, experiments, db = client.app.state.svc, client.app.state.experiments, client.app.state.db
    svc.on_finished = lambda run_id: None
    exp = created(client, body(branch_plan={"probe": {}, "placebo": None, "ablation": None}))
    parent = exp["cells"][0]["parent"]["run_id"]
    svc.wait(parent, timeout=60)
    real = db.run
    monkeypatch.setattr(db, "run", lambda rid: {**real(rid), "status": "stopped"} if rid == parent else real(rid))
    experiments.advance(exp["id"])
    assert db.run_config(exp["cells"][0]["branches"][0]["run_id"]) is None


# CR-12: the plan comes from the record, not from today's files; one blocked cell does not stop the others


def test_a_condition_file_changed_after_the_start_does_not_change_or_stop_the_experiment(client, tmp_path):
    svc, experiments = client.app.state.svc, client.app.state.experiments
    svc.on_finished = lambda run_id: None
    exp = created(client, body(branch_plan={"probe": {}, "placebo": None, "ablation": None}))
    svc.wait(exp["cells"][0]["parent"]["run_id"], timeout=60)
    path = tmp_path / "conditions" / "C1.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: draft\nfrozen_hash: null", "status: frozen\nfrozen_hash: sha256:00"))
    svc.on_finished = experiments._finished
    experiments.recover()
    done = finished(client, exp["id"])
    assert done["status"] == "completed" and done["cells"][0]["error"] is None


def test_a_cell_that_cannot_branch_is_blocked_with_its_error_and_the_other_cells_go_on(client, monkeypatch):
    from services.configs import ApiError

    svc, experiments = client.app.state.svc, client.app.state.experiments
    real = svc.branch
    blocked_parent = []

    def branch(parent, req, budget=None, ids=None):
        if parent in blocked_parent:
            raise ApiError(409, "paid_calls_disabled", "LLM agents make paid calls.")
        return real(parent, req, budget, ids)

    monkeypatch.setattr(svc, "branch", branch)
    b = body(seeds=(1, 2), branch_plan={"probe": {}, "placebo": None, "ablation": None})
    exp = created(client, b)
    blocked_parent.append(next(c["parent"]["run_id"] for c in exp["cells"] if c["seed"] == 1))
    done = finished(client, exp["id"])
    cells = {c["seed"]: c for c in done["cells"]}
    assert done["status"] == "blocked" and done["counts"] == {"blocked": 1, "completed": 1}
    assert cells[1]["status"] == "blocked" and cells[1]["error"]["code"] == "paid_calls_disabled"
    assert cells[2]["status"] == "completed" and cells[2]["error"] is None
    blocked_parent.clear()
    experiments.advance(exp["id"])
    done = finished(client, exp["id"])
    assert done["status"] == "completed" and cells[1]["parent"]["run_id"] == done["cells"][0]["parent"]["run_id"]


def test_a_branch_refuses_a_parent_whose_scenario_changed(client):
    from services.configs import ApiError
    from simcore.branch import BranchRequest
    from simschema.events import ProbeIntervention

    svc, db = client.app.state.svc, client.app.state.db
    exp = finished(client, created(client, body(branch_plan={"probe": None, "placebo": None, "ablation": None}))["id"])
    parent = exp["cells"][0]["parent"]["run_id"]
    row = db.run_config(parent)
    cfg = json.loads(row["config_json"]) | {"scenario_hash": "sha256:" + "0" * 64}
    svc.writer.submit(
        lambda conn: conn.execute("UPDATE run_configs SET config_json = ? WHERE run_id = ?", (json.dumps(cfg), parent))
    ).result()
    with pytest.raises(ApiError) as e:
        svc.branch(parent, BranchRequest(at=20, rounds=5, intervention=ProbeIntervention(target="B")))
    assert e.value.code == "hash_mismatch"


def test_under_dbos_a_stored_run_that_never_reached_the_queue_runs_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    monkeypatch.setenv("SIM_DBOS_PATH", str(tmp_path / "data" / "dbos.sqlite"))
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    with TestClient(create_app(tmp_path, use_dbos=True)) as c:
        monkeypatch.setattr(c.app.state.svc, "_launch", lambda run_id: None)  # configs stored, then the server dies
        exp = created(c, body(branch_plan={"probe": None, "placebo": None, "ablation": None}))
        rid = exp["cells"][0]["parent"]["run_id"]
        monkeypatch.undo()
        monkeypatch.setenv("SIM_DBOS_PATH", str(tmp_path / "data" / "dbos.sqlite"))
    with TestClient(create_app(tmp_path, use_dbos=True)) as c:
        assert finished(c, exp["id"])["status"] == "completed" and started_events(c, rid) == 1


def test_a_resumed_run_does_not_add_its_earlier_cost_to_the_batch_again(client, monkeypatch):
    from services.configs import RunConfig

    monkeypatch.setenv("SIM_ALLOW_PAID", "1")
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    exp = finished(
        client, created(client, body(agents=llm_agents(pid), branch_plan={"probe": None, "placebo": None, "ablation": None}))["id"]
    )
    rid = exp["cells"][0]["parent"]["run_id"]
    db, experiments = client.app.state.db, client.app.state.experiments
    batch = experiments.guard(rid)
    before = (batch.calls, batch.spent_usd)
    config = RunConfig.model_validate_json(db.run_config(rid)["config_json"])
    run_guard = client.app.state.svc.llm.budget(config, db.events(rid), batch=batch)  # what a resume builds
    usd = db.paid_calls([rid])["usd"]
    assert (batch.calls, batch.spent_usd) == before and before[1] > 0
    assert run_guard.spent_usd == pytest.approx(usd)


# CR-12 m4: the estimate is a worst case


def test_the_estimate_counts_llm_memory_summaries(client, tmp_path):
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    plan = {"probe": None, "placebo": None, "ablation": None}
    code = client.post("/api/experiments/estimate", json=body(agents=llm_agents(pid), branch_plan=plan)).json()
    path = tmp_path / "conditions" / "C1.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("memory_summary: none", "memory_summary: llm"), encoding="utf-8")
    b = body(agents=llm_agents(pid), branch_plan=plan, memory={"summary": "llm"})
    llm = check(client.post("/api/experiments/estimate", json=b), "/experiments/estimate", "post")
    summaries = stop_round(client, 1) // 5  # summary_every 5, two LLM sellers, ATTEMPTS attempts each
    assert llm["calls_max"] - code["calls_max"] == summaries * 2 * ATTEMPTS


def test_the_estimate_prices_each_model_of_a_profile_on_its_own(client):
    prof = profile_body(pricing=None, model_pricing={"m-small": {"input_usd_per_mtok": 0.1, "output_usd_per_mtok": 0.4}})
    pid = client.post("/api/provider-profiles", json=prof).json()["id"]
    agents = llm_agents(pid)
    agents[1]["policy"] = {**agents[1]["policy"], "llm": {**agents[1]["policy"]["llm"], "model": "m-large"}}
    est = check(client.post("/api/experiments/estimate", json=body(agents=agents)), "/experiments/estimate", "post")
    assert est["usd_max"] is None and est["pricing_missing_for"] == [pid]


def test_an_unpriced_experiment_whose_batch_caps_calls_needs_no_call_cap_per_run(client, monkeypatch):
    """CR-19 m3: the experiment's guard can stop every run and branch, so a run budget of max_usd only is enough."""
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    pid = client.post("/api/provider-profiles", json=profile_body(pricing=None)).json()["id"]
    b = body(agents=llm_agents(pid, both=False), budget={"max_usd": 1.0}, batch_budget={"max_calls": 5000})
    done = finished(client, created(client, b)["id"])
    (cell,) = done["cells"]
    assert cell["error"] is None and cell["status"] == "completed" and cell["branches"]


def test_an_unpriced_experiment_with_only_a_usd_batch_cap_is_refused_and_the_estimate_says_why(client):
    pid = client.post("/api/provider-profiles", json=profile_body(pricing=None)).json()["id"]
    b = body(agents=llm_agents(pid), batch_budget={"max_usd": 5}, budget={"max_calls": 100})
    est = check(client.post("/api/experiments/estimate", json=b), "/experiments/estimate", "post")
    assert any("budget_unenforceable" in w for w in est["warnings"])
    err = check(client.post("/api/experiments", json=b), "/experiments", "post")
    assert err["code"] == "budget_unenforceable" and err["errors"][0]["path"] == "batch_budget/max_calls"
    assert client.app.state.db.runs() == []


def test_the_default_plan_gives_every_condition_one_ablation_kind_child_null_where_there_is_no_private_channel(tmp_path):
    """Lead (unit 4): the missingness of ablation_effect must not carry the group."""
    from services.experiments import BranchPlan, _branch_requests

    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    catalog = Catalog(Paths(tmp_path))
    conds = Research(tmp_path / "conditions", catalog).conditions()
    catalog.close()
    assert {c.file.group for c in conds} >= {"C1", "C2a", "C2b", "C3"}
    for cond in conds:
        ablations = [r.intervention for kind, r in _branch_requests(BranchPlan(), cond, 30) if kind == "ablation"]
        assert ablations, cond.id
        if cond.file.channels.private_pairs:
            assert all(a.ablated == "channel" for a in ablations)
        else:
            assert [a.ablated for a in ablations] == ["none"]


@pytest.mark.parametrize("bad", [{"ablated": "none", "channel_id": "private_A_B"}, {"ablated": "channel"}, {"mode": "clear"}])
def test_an_ablation_names_a_channel_and_a_mode_or_neither(bad):
    from pydantic import ValidationError

    from simschema.events import AblationIntervention

    with pytest.raises(ValidationError):
        AblationIntervention(**bad)


def test_retry_forgets_the_blocks_and_advances_and_a_cause_not_fixed_blocks_again_with_a_new_time(client, monkeypatch):
    from services.configs import ApiError

    svc = client.app.state.svc
    real = svc.branch
    fixed = []

    def branch(parent, req, budget=None, ids=None):
        if not fixed:
            raise ApiError(409, "paid_calls_disabled", "LLM agents make paid calls.")
        return real(parent, req, budget, ids)

    monkeypatch.setattr(svc, "branch", branch)
    exp = created(client, body(branch_plan={"probe": {}, "placebo": None, "ablation": None}))
    first = finished(client, exp["id"])["cells"][0]["error"]
    assert first["code"] == "paid_calls_disabled"
    again = check(client.post(f"/api/experiments/{exp['id']}/retry"), "/experiments/{experiment_id}/retry", "post")
    assert again["status"] == "blocked" and again["cells"][0]["error"]["at"] > first["at"]
    fixed.append(True)
    check(client.post(f"/api/experiments/{exp['id']}/retry"), "/experiments/{experiment_id}/retry", "post")
    done = finished(client, exp["id"])
    assert done["status"] == "completed" and done["cells"][0]["error"] is None
    assert [c["kind"] for c in done["cells"][0]["branches"]] == ["probe"]
    assert client.post("/api/experiments/exp_00000000000000000000000000/retry").status_code == 404
