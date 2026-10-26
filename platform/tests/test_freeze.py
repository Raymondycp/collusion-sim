"""Research unit 3: the freeze gate and "test once" (DECISIONS-10 §4, DECISIONS-11 §2, CR-17 U3-1 to U3-7). Scripted only."""

import json
import shutil
import threading
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.app import create_app
from test_api import check
from test_judge import RUBRIC_TEXT, write_models
from test_research import ROOT, research_config
from condcopy import draft_conditions

TEST_SEED = 201


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    return tmp_path


@pytest.fixture
def client(root):
    with TestClient(create_app(root, use_dbos=False)) as c:
        yield c


def rubric_file(root: Path) -> None:
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")  # stage conditions parses it
    write_models(root / "conditions")


def rule_file(root: Path) -> None:
    (root / "conditions" / "decision-rule.yaml").write_text("rule: corr_lag > 0.4\n", encoding="utf-8")


def research_files(root: Path) -> None:
    rubric_file(root)
    rule_file(root)


def freeze(client, stage: str, who: str = "lead"):
    return client.post("/api/freeze", json={"stage": stage, "approved_by": who})


def frozen(client, root: Path) -> dict:
    """Both stages: conditions (opens dev), then rule (opens test)."""
    research_files(root)
    check(freeze(client, "conditions"), "/freeze", "post")
    return check(freeze(client, "rule", "p2"), "/freeze", "post")


def split_test_config(**kw) -> dict:
    return research_config(split="test", seeds=(TEST_SEED,), **kw)


def experiment(**kw) -> dict:
    return {
        "conditions": ["C1"],
        "split": "test",
        "seeds": [TEST_SEED],
        "agents": research_config()["agents"],
        "budget": {"max_usd": 1.0},
        "batch_budget": {"max_calls": 1000},
        "branch_plan": {"probe": None, "placebo": {"n": 1}, "ablation": None},
        **kw,
    }


def code(r) -> tuple[int, str | None]:
    return r.status_code, r.json().get("code") if r.status_code >= 400 else None


# U3-7: the freeze


def _all_draft(root: Path) -> bool:
    return all(yaml.safe_load(p.read_text(encoding="utf-8"))["status"] == "draft" for p in (root / "conditions").glob("C*.yaml"))


def test_stage_conditions_needs_the_rubric_and_stage_rule_needs_stage_conditions_and_the_rule(client, root):
    r = check(freeze(client, "rule"), "/freeze", "post")
    assert (r["status"], r["code"]) == (409, "conflict") and not (root / "conditions" / "frozen.json").exists()
    r = check(freeze(client, "conditions"), "/freeze", "post")
    assert r["code"] == "freeze_incomplete" and "judge-rubric.yaml" in r["message"] and "decision-rule.yaml" not in r["message"]
    assert _all_draft(root) and not (root / "conditions" / "frozen.json").exists()
    rubric_file(root)
    check(freeze(client, "conditions"), "/freeze", "post")
    r = check(freeze(client, "rule"), "/freeze", "post")
    assert r["code"] == "freeze_incomplete" and "decision-rule.yaml" in r["message"] and "judge-rubric.yaml" not in r["message"]
    assert freeze(client, "conditions", "").status_code == 422 and freeze(client, "all").status_code == 422


def test_stage_conditions_opens_dev_and_keeps_test_locked_and_stage_rule_opens_test(client, root):
    assert code(client.post("/api/runs", json=research_config(split="dev", seeds=(101,)))) == (409, "condition_not_frozen")
    rubric_file(root)
    status = check(freeze(client, "conditions"), "/freeze", "post")
    assert status["stages"]["conditions"]["valid"] is True and status["valid"] is False
    assert status["problems"] == ["stage rule is not frozen"]
    assert "decision_rule" not in status["hashes"] and "judge_rubric" in status["hashes"]
    assert client.post("/api/runs", json=research_config(split="dev", seeds=(101,))).status_code == 202
    assert code(client.post("/api/runs", json=split_test_config())) == (409, "test_split_locked")
    assert check(client.get("/api/seeds"), "/seeds")["test_locked"] is True
    rule_file(root)
    _edit(root, "decision-rule.yaml", "0.4", "0.45")  # tuned on dev before the rule stage
    status = check(freeze(client, "rule", "p2"), "/freeze", "post")
    assert status["valid"] is True and status["stages"]["rule"]["approved_by"] == "p2"
    assert client.post("/api/runs", json=split_test_config()).status_code == 202


def test_a_conditions_change_after_the_rule_stage_locks_dev_and_test(client, root):
    frozen(client, root)
    _edit(root, "prompts.draft.yaml", "free to coordinate", "free to talk")
    status = check(client.get("/api/freeze"), "/freeze")
    assert status["stages"]["conditions"]["valid"] is False and status["valid"] is False
    assert "condition:C1 changed since the freeze" in status["problems"]


def test_the_freeze_sets_each_condition_frozen_with_its_hash_and_writes_every_hash(client, root):
    status = frozen(client, root)
    assert status["valid"] is True and status["problems"] == []
    for c in client.get("/api/conditions").json():
        assert c["status"] == "frozen" and c["frozen_hash"] == c["hash"]
    stages = json.loads((root / "conditions" / "frozen.json").read_text(encoding="utf-8"))["stages"]
    assert (stages["conditions"]["approved_by"], stages["rule"]["approved_by"]) == ("lead", "p2")
    assert stages["rule"]["hashes"] == status["hashes"]
    assert stages["conditions"]["hashes"] == {k: v for k, v in status["hashes"].items() if k != "decision_rule"}
    keys = set(status["hashes"])
    assert {"seeds", "features", "decision_rule", "judge_rubric", "condition:C1", f"scenario:scn_s{TEST_SEED}"} <= keys
    assert check(client.get("/api/seeds"), "/seeds")["test_locked"] is False
    assert check(client.get("/api/freeze"), "/freeze")["valid"] is True


# U3-1: the gate computes every hash again; a stored flag is never enough


def _edit(root: Path, name: str, old: str, new: str) -> None:
    path = root / "conditions" / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


EDITS = {
    "condition:C1": lambda root, mp: _edit(root, "prompts.draft.yaml", "free to coordinate", "free to talk"),
    "decision_rule": lambda root, mp: _edit(root, "decision-rule.yaml", "0.4", "0.5"),
    "judge_rubric": lambda root, mp: _edit(root, "judge-rubric.yaml", "offer", "proposal"),
    "features": lambda root, mp: mp.setattr("features.base.source_hash", lambda roots, src=None: "0" * 64),
}


@pytest.mark.parametrize("key", sorted(EDITS))
def test_an_edit_after_the_freeze_locks_the_test_seeds_again_and_the_status_names_it(client, root, monkeypatch, key):
    frozen(client, root)
    EDITS[key](root, monkeypatch)
    status = check(client.get("/api/freeze"), "/freeze")
    assert status["valid"] is False and f"{key} changed since the freeze" in status["problems"]
    r = client.post("/api/runs", json=split_test_config())
    assert code(r) == (409, "test_split_locked") and key in r.json()["message"]
    assert check(client.get("/api/seeds"), "/seeds")["test_locked"] is True
    dev = code(client.post("/api/runs", json=research_config(split="dev", seeds=(101,))))
    want = {"decision_rule": (202, None), "condition:C1": (409, "hash_mismatch")}  # the file's frozen_hash check comes first
    assert dev == want.get(key, (409, "condition_not_frozen"))  # the rubric is a dev input (L1)


def test_a_condition_set_back_to_draft_breaks_the_freeze(client, root):
    frozen(client, root)
    _edit(root, "C2a.yaml", "\nstatus: frozen", "\nstatus: draft")
    _edit(root, "C2a.yaml", "\nfrozen_hash: ", "\nfrozen_hash: null\n# was: ")
    assert "condition:C2a is not frozen with its hash" in check(client.get("/api/freeze"), "/freeze")["problems"]


# U3-2: every way to a test seed passes the same gate


def _start(client, path, body):
    return client.post(path, json=body)


PATHS = {
    "POST /runs": lambda c: _start(c, "/api/runs", split_test_config()),
    "POST /runs/estimate": lambda c: _start(c, "/api/runs/estimate", split_test_config()),
    "POST /experiments": lambda c: _start(c, "/api/experiments", experiment()),
    "POST /experiments/estimate": lambda c: _start(c, "/api/experiments/estimate", experiment()),
    "exploratory run on a test scenario": lambda c: _start(c, "/api/runs", _exploratory(c)),
    "exploratory estimate on a test scenario": lambda c: _start(c, "/api/runs/estimate", _exploratory(c)),
}


def _exploratory(c) -> dict:
    from test_api import config

    entry = c.app.state.research.seed_map().entry(TEST_SEED)
    return config(seeds=(TEST_SEED,)) | {"scenario_id": entry.scenario_id, "scenario_hash": entry.scenario_hash}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_each_path_to_a_test_seed_is_locked_before_the_freeze_and_open_after_it(client, root, path):
    assert code(PATHS[path](client)) == (409, "test_split_locked")
    assert client.app.state.db.runs() == [] and client.app.state.db.experiments() == []
    frozen(client, root)
    r = PATHS[path](client)
    assert r.status_code < 300, r.text


def _invalidate(root: Path) -> None:
    _edit(root, "decision-rule.yaml", "0.4", "0.5")


def test_a_branch_of_a_test_parent_needs_a_valid_freeze(client, root):
    frozen(client, root)
    rid = check(client.post("/api/runs", json=split_test_config()), "/runs", "post")["run_ids"][0]
    client.app.state.svc.wait(rid, timeout=60)
    _invalidate(root)
    r = client.post(f"/api/runs/{rid}/branches", json={"at": 5, "rounds": 2, "intervention": {"type": "placebo"}})
    assert code(r) == (409, "test_split_locked")


def test_advance_does_not_start_a_planned_test_parent_after_the_freeze_broke(client, root, monkeypatch):
    frozen(client, root)
    monkeypatch.setattr(client.app.state.svc, "_launch", lambda run_id: None)
    monkeypatch.setattr(client.app.state.svc, "commit", lambda prepared: None)  # the server dies after the plan is stored
    exp = check(client.post("/api/experiments", json=experiment()), "/experiments", "post")
    monkeypatch.undo()
    _invalidate(root)
    client.app.state.experiments.advance(exp["id"])
    cell = check(client.get(f"/api/experiments/{exp['id']}"), "/experiments/{experiment_id}")["cells"][0]
    assert cell["status"] == "blocked" and cell["error"]["code"] == "test_split_locked"
    assert client.app.state.db.run_config(cell["parent"]["run_id"]) is None


def test_the_feature_export_of_a_test_experiment_needs_a_valid_freeze(client, root):
    frozen(client, root)
    exp = check(client.post("/api/experiments", json=experiment()), "/experiments", "post")
    client.app.state.experiments.wait(exp["id"], timeout=120)
    assert client.get(f"/api/experiments/{exp['id']}/features").status_code == 200
    _invalidate(root)
    assert code(client.get(f"/api/experiments/{exp['id']}/features")) == (409, "test_split_locked")


def test_the_run_export_of_a_test_experiment_needs_a_valid_freeze(client, root):
    frozen(client, root)
    exp = check(client.post("/api/experiments", json=experiment()), "/experiments", "post")
    client.app.state.experiments.wait(exp["id"], timeout=120)
    assert client.get(f"/api/experiments/{exp['id']}/export", params={"format": "zip"}).status_code == 200
    _invalidate(root)
    assert code(client.get(f"/api/experiments/{exp['id']}/export", params={"format": "zip"})) == (409, "test_split_locked")


def test_the_zip_export_of_a_test_run_needs_a_valid_freeze(client, root):
    frozen(client, root)
    rid = check(client.post("/api/runs", json=split_test_config()), "/runs", "post")["run_ids"][0]
    client.app.state.svc.wait(rid, timeout=60)
    assert client.get(f"/api/runs/{rid}/export", params={"format": "zip"}).status_code == 200
    _invalidate(root)
    assert code(client.get(f"/api/runs/{rid}/export", params={"format": "zip"})) == (409, "test_split_locked")
    assert code(client.get(f"/api/runs/{rid}/calls/1/trail")) == (409, "test_split_locked"), "the trail route has the same gate"


# U3-3, U3-4, U3-5: a test cell runs once


def test_a_test_cell_runs_once_even_after_its_run_was_stopped(client, root):
    frozen(client, root)
    rid = check(client.post("/api/runs", json=split_test_config()), "/runs", "post")["run_ids"][0]
    client.post(f"/api/runs/{rid}/stop")
    client.app.state.svc.wait(rid, timeout=60)
    for path in ("/api/runs", "/api/runs/estimate"):
        r = client.post(path, json=split_test_config())
        assert code(r) == (409, "test_seed_already_run") and rid in r.json()["message"], path
    assert code(client.post("/api/experiments", json=experiment())) == (409, "test_seed_already_run")
    assert client.app.state.db.test_cell("C1", TEST_SEED) == rid and len(client.app.state.db.runs()) == 1


def test_two_parallel_starts_of_one_test_cell_store_one_run(client, root, monkeypatch):
    frozen(client, root)
    svc = client.app.state.svc
    real, barrier = svc.prepare, threading.Barrier(2)

    def prepare(config, run_ids=None):
        out = real(config, run_ids)
        barrier.wait(timeout=10)  # both requests pass every read check before either stores
        return out

    monkeypatch.setattr(svc, "prepare", prepare)
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.post("/api/runs", json=split_test_config()))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.status_code for r in results) == [202, 409]
    assert [code(r) for r in results if r.status_code == 409] == [(409, "test_seed_already_run")]
    assert len(client.app.state.db.runs()) == 1 and client.app.state.db.test_cell_count() == 1


def test_a_test_experiment_resumed_after_a_crash_keeps_one_lock_and_completes(client, root, monkeypatch):
    frozen(client, root)
    svc, experiments = client.app.state.svc, client.app.state.experiments
    monkeypatch.setattr(svc, "_launch", lambda run_id: None)  # stored and locked, then the server dies
    exp = check(client.post("/api/experiments", json=experiment()), "/experiments", "post")
    monkeypatch.undo()
    parent = exp["cells"][0]["parent"]["run_id"]
    assert client.app.state.db.test_cell("C1", TEST_SEED) == parent
    experiments.recover()
    svc.commit(svc.prepare(experiments._body(exp["id"]).run_config("C1"), [parent]))  # a second commit of the same id
    done = experiments.wait(exp["id"], timeout=120)
    assert done["status"] == "completed" and client.app.state.db.test_cell_count() == 1


def test_the_freeze_cannot_change_after_a_test_cell_is_used(client, root):
    frozen(client, root)
    check(client.post("/api/runs", json=split_test_config()), "/runs", "post")
    for stage in ("conditions", "rule"):
        assert code(freeze(client, stage)) == (409, "conflict")


def test_the_lock_rows_cannot_be_deleted_or_changed(client, root):
    frozen(client, root)
    rid = check(client.post("/api/runs", json=split_test_config()), "/runs", "post")["run_ids"][0]
    writer = client.app.state.writer if hasattr(client.app.state, "writer") else client.app.state.svc.writer
    for sql in ("DELETE FROM test_cells", "UPDATE test_cells SET run_id = 'x'"):
        with pytest.raises(Exception, match="used once"):
            writer.submit(lambda conn, sql=sql: conn.execute(sql)).result()
    assert client.app.state.db.test_cell("C1", TEST_SEED) == rid


# CR-26 m1, m2, m3


def _changed_feature_code(monkeypatch) -> None:
    monkeypatch.setattr("features.base.source_hash", lambda roots, src=None: "0" * 64)


def test_a_feature_file_changed_since_the_start_blocks_the_freeze_and_the_export(client, root, monkeypatch):
    frozen(client, root)
    exp = check(client.post("/api/experiments", json=experiment()), "/experiments", "post")
    client.app.state.experiments.wait(exp["id"], timeout=120)
    _changed_feature_code(monkeypatch)
    assert code(client.get(f"/api/experiments/{exp['id']}/features")) == (409, "features_changed")


def test_a_feature_file_changed_since_the_start_blocks_the_freeze(client, monkeypatch):
    _changed_feature_code(monkeypatch)
    r = freeze(client, "conditions")
    assert code(r) == (409, "features_changed") and "Restart the server" in r.json()["message"]


def test_an_experiment_with_an_llm_condition_after_a_scripted_one_meets_the_paid_switch(client, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    llm = {"kind": "llm", "llm": {"provider_profile_id": "prof_x", "model": "m"}}
    agents = [{"id": x, "role": "seller", "policy": llm} for x in "AB"]
    body = experiment(conditions=["C1", "C2a"], split="pilot", seeds=[1], agents_by_condition={"C2a": agents})
    assert code(client.post("/api/experiments", json=body)) == (409, "paid_calls_disabled")
    assert client.app.state.db.experiments() == []


def test_an_exploratory_run_on_a_copy_of_a_test_scenario_waits_for_the_freeze(client, root):
    from test_api import config

    catalog = client.app.state.catalog
    from services.catalog import DRAFT_FIELDS

    content = catalog.scenario(f"scn_s{TEST_SEED}").content()
    catalog.create("scn_copy", {k: v for k, v in content.items() if k in DRAFT_FIELDS} | {"name": "a copy", "provenance": None})
    copy = catalog.freeze("scn_copy", "someone")
    body = config(seeds=(TEST_SEED,)) | {"scenario_id": "scn_copy", "scenario_hash": copy["hash"]}
    assert code(client.post("/api/runs", json=body)) == (409, "test_split_locked")
    frozen(client, root)
    assert client.post("/api/runs", json=body).status_code == 202


# CR-29 m1, m3


def test_the_rule_may_change_after_its_stage_until_a_test_cell_is_used_and_the_change_locks_test(client, root):
    frozen(client, root)
    saved = check(client.put("/api/research/decision-rule", json={"text": "rule: corr_lag > 0.5\n"}), "/research/{name}", "put")
    assert saved["invalidated_stage"] == "rule"
    status = check(client.get("/api/freeze"), "/freeze")
    assert status["valid"] is False and status["stages"]["conditions"]["valid"] is True
    assert code(client.post("/api/runs", json=split_test_config())) == (409, "test_split_locked")
    again = check(client.put("/api/research/decision-rule", json={"text": "rule: corr_lag > 0.6\n"}), "/research/{name}", "put")
    assert again["invalidated_stage"] is None  # the stage was already invalid
    check(freeze(client, "rule", "p2"), "/freeze", "post")
    check(client.post("/api/runs", json=split_test_config()), "/runs", "post")
    r = check(client.put("/api/research/decision-rule", json={"text": "rule: other\n"}), "/research/{name}", "put")
    assert (r["status"], r["code"]) == (409, "research_frozen") and "test cells are used" in r["message"]
    assert (root / "conditions" / "decision-rule.yaml").read_text(encoding="utf-8") == "rule: corr_lag > 0.6\n"


def test_the_rubric_cannot_change_once_stage_conditions_is_frozen(client, root):
    rubric_file(root)
    check(freeze(client, "conditions"), "/freeze", "post")
    r = check(client.put("/api/research/judge-rubric", json={"text": "items: [offer]\n"}), "/research/{name}", "put")
    assert (r["status"], r["code"]) == (409, "research_frozen") and "stage conditions" in r["message"]


def _dev_experiment() -> dict:
    return experiment(split="dev", seeds=[101])


def test_advance_does_not_start_a_planned_dev_parent_after_stage_conditions_broke(client, root, monkeypatch):
    rubric_file(root)
    check(freeze(client, "conditions"), "/freeze", "post")
    monkeypatch.setattr(client.app.state.svc, "_launch", lambda run_id: None)
    monkeypatch.setattr(client.app.state.svc, "commit", lambda prepared: None)
    exp = check(client.post("/api/experiments", json=_dev_experiment()), "/experiments", "post")
    monkeypatch.undo()
    _edit(root, "prompts.draft.yaml", "free to coordinate", "free to talk")
    client.app.state.experiments.advance(exp["id"])
    cell = check(client.get(f"/api/experiments/{exp['id']}"), "/experiments/{experiment_id}")["cells"][0]
    assert cell["status"] == "blocked" and cell["error"]["code"] == "condition_not_frozen"
    assert client.app.state.db.run_config(cell["parent"]["run_id"]) is None


def test_a_branch_of_a_dev_parent_needs_a_valid_stage_conditions(client, root):
    rubric_file(root)
    check(freeze(client, "conditions"), "/freeze", "post")
    rid = check(client.post("/api/runs", json=research_config(split="dev", seeds=(101,))), "/runs", "post")["run_ids"][0]
    client.app.state.svc.wait(rid, timeout=60)
    _edit(root, "judge-rubric.yaml", "offer", "proposal")
    r = client.post(f"/api/runs/{rid}/branches", json={"at": 5, "rounds": 2, "intervention": {"type": "placebo"}})
    assert code(r) == (409, "condition_not_frozen")


@pytest.mark.parametrize(
    ("rule", "path"),
    [
        ({"features_allowed": ["corr_lag", "timeout_fallbacks"]}, "features_allowed/1"),
        ({"candidate_rules": [{"id": "r1", "features": ["degraded_calls"]}]}, "candidate_rules/0/features/0"),
    ],
)
def test_stage_rule_refuses_a_quality_feature_as_a_detector(client, root, rule, path):
    """DECISIONS-23 entry 2: run-quality columns may sit in the manipulation check, never in a detector place."""
    rubric_file(root)
    check(freeze(client, "conditions"), "/freeze", "post")
    check_rule = {"manipulation_check": {"C1": [{"feature": "timeout_fallbacks", "operator": ">", "value": 0}]}}
    (root / "conditions" / "decision-rule.yaml").write_text(yaml.safe_dump({**check_rule, **rule}), encoding="utf-8")
    r = check(freeze(client, "rule", "p2"), "/freeze", "post")
    assert (r["status"], r["code"]) == (409, "freeze_incomplete") and [e["path"] for e in r["errors"]] == [path]
    (root / "conditions" / "decision-rule.yaml").write_text(yaml.safe_dump(check_rule), encoding="utf-8")
    assert freeze(client, "rule", "p2").status_code == 200, "a quality feature in the check alone is fine"
