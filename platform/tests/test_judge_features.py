# ruff: noqa: F811 (the fixtures root and client come from test_llm_judge)
"""Item 3 unit J4: the LLM judge's scores as columns of the experiment feature table. Fake endpoint; no paid call."""

import json

import pytest

from services import features as features_svc
from services.configs import ApiError
from test_freeze import TEST_SEED, rule_file
from test_judge import _talkers
from test_llm_judge import body, client, root, text  # noqa: F401 (fixtures)


def agents(condition: str) -> list[dict]:
    return [{"id": s, "role": "seller", "policy": {"kind": k, "params": p}} for s, (k, p) in _talkers(condition).items()]


AGENTS = agents("C1")


def experiment(client, seeds, condition="C1", split="pilot") -> str:
    req = {"conditions": [condition], "split": split, "seeds": list(seeds), "agents": agents(condition), "budget": {"max_usd": 1.0}}
    exp = client.post("/api/experiments", json=req | {"batch_budget": {"max_calls": 1000}}).json()
    client.app.state.experiments.wait(exp["id"], timeout=120)
    return exp["id"]


def parents(client, exp_id) -> list[str]:
    return sorted(r["run_id"] for r in client.app.state.db.experiment_runs(exp_id) if r["kind"] == "parent")


def feature_table(client, exp_id, evaluation):
    """evaluation: one evaluation detail, a list of them, or None."""
    s = client.app.state
    evaluations = [] if evaluation is None else evaluation if isinstance(evaluation, list) else [evaluation]
    return features_svc.table(s.db, s.research.catalog, s.research, exp_id, "B", evaluations)


def test_the_judge_columns_join_one_evaluation_per_parent_episode(client):
    exp_id = experiment(client, seeds=(1, 2))
    judged_run, unjudged_run = parents(client, exp_id)
    client.brain.script = [text("not json"), text("still not json")]  # one paraphrase ends invalid
    svc = client.app.state.evaluations
    job = svc.wait(svc.create(body(client, [judged_run]))["id"])
    meta, columns, rows = feature_table(client, exp_id, job)
    for f in ("llm_agreement", "llm_agreement_share", "llm_talk_score", "llm_n_ok"):
        assert f in columns and f"{f}_missing" in columns
    by_run = {r["parent_run_id"]: r for r in rows}
    llm = job["episodes"][0]["llm"]
    got = by_run[judged_run]
    assert (got["llm_agreement"], got["llm_talk_score"], got["llm_n_ok"]) == (llm["agreement"], llm["talk_score"], 2)
    assert got["llm_agreement"] == 0 and got["llm_agreement_missing"] is None
    assert by_run[unjudged_run]["llm_talk_score"] is None and by_run[unjudged_run]["llm_talk_score_missing"] == "not_judged"
    assert [e["evaluation_id"] for e in meta["judge"]["evaluations"]] == [job["id"]] and meta["judge"]["item_set"] == job["item_set"]
    assert got["judge_evaluation_id"] == job["id"] and by_run[unjudged_run]["judge_evaluation_id"] is None
    assert meta["judge"]["invalid_rate_by_group"] == {got["group"]: 2 / 4}  # 2 invalid of 4 results; sent rows left out


def test_the_table_without_an_evaluation_has_no_judge_columns(client):
    exp_id = experiment(client, seeds=(1,))
    meta, columns, _ = feature_table(client, exp_id, None)
    assert "judge" not in meta and not any(c.startswith("llm_") for c in columns)


def test_an_evaluation_with_no_llm_backend_gives_no_judge_columns(client):
    exp_id = experiment(client, seeds=(1,))
    svc = client.app.state.evaluations
    scripted = svc.wait(svc.create(body(client, parents(client, exp_id), backends={"scripted": {}}))["id"])
    with pytest.raises(ApiError) as e:
        feature_table(client, exp_id, scripted)
    assert (e.value.status, e.value.code, e.value.errors[0]["path"]) == (422, "validation_error", "evaluation_id/0")


# CR-37 fixes


def one_row(client, exp_id, job) -> dict:
    _, _, rows = feature_table(client, exp_id, job)
    return rows[0]


def test_the_judge_columns_are_declared_as_layer_a_log_access_features_in_the_layer_a_table(client):
    exp_id = experiment(client, seeds=(1,))
    svc = client.app.state.evaluations
    job = svc.wait(svc.create(body(client, parents(client, exp_id)))["id"])
    s = client.app.state
    meta, columns, rows = features_svc.table(s.db, s.research.catalog, s.research, exp_id, "A", [job])
    specs = {f["id"]: f for f in meta["features"] if f["id"].startswith("llm_")}
    assert sorted(specs) == ["llm_agreement", "llm_agreement_share", "llm_n_ok", "llm_talk_score"]
    assert {(f["data_layer"], f["access"], f["evidence_type"], f["version"]) for f in specs.values()} == {("A", "log", "conversation", "1")}
    assert all(f["code_hash"] and f["inputs"] == ["posted"] for f in specs.values())
    assert "llm_talk_score" in columns and rows[0]["llm_n_ok"] == 3


def test_the_freeze_hash_covers_the_score_files_but_not_their_imports_or_the_job_runner(tmp_path, monkeypatch):
    from features import REGISTRY, base

    imported = "simcore.run"

    def path(name):
        return tmp_path.joinpath(*name.split(".")).with_suffix(".py")

    for name in (*base.JUDGE_CODE, imported):
        path(name).parent.mkdir(parents=True, exist_ok=True)
        path(name).write_text(f"import {imported}\n", encoding="utf-8")
    before = base.files_hash(base.JUDGE_CODE, tmp_path)
    path(imported).write_text("x = 2\n", encoding="utf-8")
    assert base.files_hash(base.JUDGE_CODE, tmp_path) == before
    for name in base.JUDGE_CODE:
        path(name).write_text(f"import {imported}\ny = 1\n", encoding="utf-8")
        assert base.files_hash(base.JUDGE_CODE, tmp_path) != before, name
        path(name).write_text(f"import {imported}\n", encoding="utf-8")
    assert set(base.JUDGE_CODE) == {
        "judge.aggregate",
        "judge.blocks",
        "judge.items",
        "judge.llm_judge",
        "judge.scoring",
        "judge.transcript",
        "judge.verify",
        "judge.views",
    }  # not services.evaluations, services.features or services.judging: a runner or listing fix does not lock dev and test
    hashed = REGISTRY.code_hash()
    monkeypatch.setattr(base, "files_hash", lambda names, src=base.SRC: "changed")
    assert REGISTRY.code_hash() != hashed  # the score files are part of features_code_hash


def test_a_split_vote_is_a_tie_and_no_ok_call_is_its_own_reason(client):
    exp_id = experiment(client, seeds=(1, 2))
    rid, other = parents(client, exp_id)
    lines = client.app.state.judge.rendered(rid, "talk_only", "none").lines
    offer, accept = lines[0], next(ln for ln in lines if ln.sender != lines[0].sender)
    quotes = [
        {"role": "offer", "round": offer.round, "sender": offer.sender, "text": offer.text},
        {"role": "acceptance", "round": accept.round, "sender": accept.sender, "text": accept.text},
    ]
    agree = json.dumps({"quotes": quotes, "commitments": [], "rationale": "Offer and acceptance.", "agreement": 1, "talk_score": 4})
    client.brain.script = [text(agree), text("no"), text("no")]  # votes 1, then one paraphrase invalid twice, then 0
    svc = client.app.state.evaluations
    rows = {r["parent_run_id"]: r for r in feature_table(client, exp_id, svc.wait(svc.create(body(client, [rid]))["id"]))[2]}
    tie = rows[rid]
    assert (tie["llm_agreement"], tie["llm_agreement_missing"], tie["llm_n_ok"], tie["llm_agreement_share"]) == (None, "tie", 2, 0.5)
    client.brain.script = [text("no")] * 6  # every paraphrase invalid, then invalid again on its retry
    rows = {r["parent_run_id"]: r for r in feature_table(client, exp_id, svc.wait(svc.create(body(client, [other]))["id"]))[2]}
    none_ok = rows[other]
    assert (none_ok["llm_talk_score"], none_ok["llm_talk_score_missing"], none_ok["llm_n_ok"]) == (None, "no_ok_call", 0)


def test_an_episode_with_no_talk_is_missing_no_talk_not_a_judge_failure(client):
    exp_id = experiment(client, seeds=(1,), condition="C2a")
    svc = client.app.state.evaluations
    job = svc.wait(svc.create(body(client, parents(client, exp_id)))["id"])
    row = one_row(client, exp_id, job)
    assert {row[f"{f}_missing"] for f in features_svc.JUDGE_FEATURES} == {"no_talk"} and row["llm_talk_score"] is None
    assert client.fake.bodies == []


def test_test_rows_with_judge_columns_need_a_valid_freeze(client, root):
    rule_file(root)
    for stage in ("conditions", "rule"):
        assert client.post("/api/freeze", json={"stage": stage, "approved_by": "lead"}).status_code == 200
    exp_id = experiment(client, seeds=(TEST_SEED,), split="test")
    svc = client.app.state.evaluations
    job = svc.wait(svc.create(body(client, parents(client, exp_id), block="main"))["id"])
    assert one_row(client, exp_id, job)["llm_n_ok"] == 3
    prompts = root / "conditions" / "prompts.draft.yaml"
    prompts.write_text(prompts.read_text(encoding="utf-8").replace("free to coordinate", "free to talk"), encoding="utf-8")
    with pytest.raises(ApiError) as e:
        feature_table(client, exp_id, job)
    assert (e.value.status, e.value.code) == (409, "test_split_locked")


def test_the_route_joins_the_judge_columns_and_follows_the_contract(client):
    from test_api import check as contract

    exp_id = experiment(client, seeds=(1,))
    svc = client.app.state.evaluations
    job = svc.wait(svc.create(body(client, parents(client, exp_id)))["id"])
    path = "/experiments/{experiment_id}/features"
    got = contract(client.get(f"/api/experiments/{exp_id}/features", params={"evaluation_id": job["id"]}), path)
    assert got["meta"]["judge"]["evaluations"][0]["evaluation_id"] == job["id"] and got["rows"][0]["llm_n_ok"] == 3
    csv = client.get(f"/api/experiments/{exp_id}/features", params={"evaluation_id": job["id"], "format": "csv"}).text
    assert "llm_talk_score" in csv.splitlines()[0]
    missing = contract(client.get(f"/api/experiments/{exp_id}/features", params={"evaluation_id": "eval_" + "0" * 26}), path)
    assert missing["status"] == 404


# CR-37 addendum (lead, 22 Oct)


def test_an_episode_with_no_llm_result_yet_is_not_judged_not_no_talk():
    from judge.scoring import JUDGE_FEATURES, judge_columns

    llm = {"n_ok": 0, "agreement": None, "agreement_share": None, "talk_score": None}
    sent = {"backend": "llm", "status": "sent"}
    for calls in ([], [sent]):
        got = judge_columns({"run_id": "r", "calls": calls, "llm": llm})
        assert {got[f"{f}_missing"] for f in JUDGE_FEATURES} == {"not_judged"}, calls
    no_talk = judge_columns({"run_id": "r", "calls": [{"backend": "llm", "status": "no_talk"}], "llm": llm})
    assert no_talk["llm_n_ok_missing"] == "no_talk"


def test_an_item_set_with_the_placeholder_text_is_refused_with_its_path(client, root):
    import yaml

    rubric = root / "conditions" / "judge-rubric.yaml"
    data = yaml.safe_load(rubric.read_text(encoding="utf-8"))
    data["items"]["offer"]["text"] = "TEAM TO WRITE: the offer item."
    data["llm"]["paraphrases"][1]["system"] += " TEAM TO WRITE"
    rubric.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    listed = client.app.state.judge.item_sets()[0]
    assert not listed["valid"] and [p["path"] for p in listed["problems"]] == ["items/offer/text", "llm/paraphrases/1/system"]
    exp_id = experiment(client, seeds=(1,))
    with pytest.raises(ApiError) as e:
        client.app.state.evaluations.create(body(client, parents(client, exp_id)))
    assert (e.value.status, e.value.code) == (422, "validation_error")
    assert [x["message"].split(":")[0] for x in e.value.errors] == ["items/offer/text", "llm/paraphrases/1/system"]


def test_the_rule_stage_refuses_a_decision_rule_with_the_placeholder_text(client, root):
    (root / "conditions" / "decision-rule.yaml").write_text("rule: TEAM TO WRITE\n", encoding="utf-8")
    assert client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"}).status_code == 200
    r = client.post("/api/freeze", json={"stage": "rule", "approved_by": "lead"})
    assert (r.status_code, r.json()["code"]) == (409, "freeze_incomplete") and "TEAM TO WRITE" in r.json()["message"]
    assert not client.get("/api/freeze").json()["stages"]["rule"]["frozen"]
    rule_file(root)
    assert client.post("/api/freeze", json={"stage": "rule", "approved_by": "lead"}).status_code == 200


@pytest.mark.parametrize(
    ("change", "paths"),
    [
        (lambda d: d["items"]["offer"].update(text="TEAM TO WRITE"), ["items/offer/text"]),
        (lambda d: d["items"].pop("acceptance"), ["items/acceptance"]),
    ],
    ids=["placeholder", "bad_shape"],
)
def test_stage_conditions_refuses_a_rubric_that_is_not_a_valid_item_set(client, root, change, paths):
    import yaml

    rubric = root / "conditions" / "judge-rubric.yaml"
    data = yaml.safe_load(rubric.read_text(encoding="utf-8"))
    change(data)
    rubric.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    r = client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"})
    assert (r.status_code, r.json()["code"]) == (409, "freeze_incomplete") and "judge-rubric.yaml" in r.json()["message"]
    assert [e["path"] for e in r.json()["errors"]] == paths
    assert not (root / "conditions" / "frozen.json").exists()


# CR-64 finding 2 (lead option a): several evaluations merge into one set of judge columns


def two_jobs(client, **second) -> tuple[str, list[dict]]:
    exp_id = experiment(client, seeds=(1, 2))
    svc = client.app.state.evaluations
    first, other = parents(client, exp_id)
    jobs = [svc.wait(svc.create(body(client, [rid], **kw))["id"]) for rid, kw in ((first, {}), (other, second))]
    return exp_id, jobs


def test_evaluations_with_one_item_set_view_and_model_and_disjoint_parents_merge(client):
    exp_id, jobs = two_jobs(client)
    meta, columns, rows = feature_table(client, exp_id, jobs)
    assert columns.count("judge_evaluation_id") == 1 and columns.count("llm_n_ok") == 1
    source = {r["parent_run_id"]: r["judge_evaluation_id"] for r in rows}
    assert source == {j["run_ids"][0]: j["id"] for j in jobs}
    assert all(r["llm_n_ok"] == 3 for r in rows)
    assert [(e["evaluation_id"], e["block"], e["episodes"]) for e in meta["judge"]["evaluations"]] == [
        (j["id"], "wording", 1) for j in jobs
    ]
    assert meta["judge"]["wordings_by_group"] == {"C1": [3]}


def test_evaluations_that_judge_one_parent_twice_or_differ_in_view_are_refused_with_the_index(client):
    exp_id, jobs = two_jobs(client)
    svc = client.app.state.evaluations
    again = svc.wait(svc.create(body(client, jobs[0]["run_ids"]))["id"])
    with pytest.raises(ApiError) as e:
        feature_table(client, exp_id, [*jobs, again])
    assert (e.value.status, e.value.errors[0]["path"]) == (422, "evaluation_id/2") and jobs[0]["id"] in e.value.message
    exp_id, jobs = two_jobs(client, view="talk_prices")
    with pytest.raises(ApiError) as e:
        feature_table(client, exp_id, jobs)
    assert (e.value.status, e.value.errors[0]["path"]) == (422, "evaluation_id/1") and "view" in e.value.message


def test_the_judge_meta_lists_the_wordings_by_report_group():
    """CR-64 finding 3: C3-silent is judged with one wording, the main groups with three; the P2 report reads it per group."""

    def episode(rid, wordings):
        return {"run_id": rid, "calls": [{"backend": "llm", "status": "ok", "exploratory": False, "paraphrase": p} for p in wordings]}

    def job(eid, block, episodes):
        llm = {"model": "m", "model_family": "f"}
        return {"id": eid, "status": "completed", "block": block, "view": "talk_only", "item_set": {"id": "i", "hash": "h"},
                "backends": {"llm": llm}, "run_ids": [e["run_id"] for e in episodes], "episodes": episodes}  # fmt: skip

    rows = [{"parent_run_id": "a", "group": "C1", "sub_condition": None},
            {"parent_run_id": "s", "group": "C3", "sub_condition": "responder_silent"}]  # fmt: skip
    evaluations = [job("e1", "main", [episode("a", (1, 2, 3))]), job("e2", "support_one_wording", [episode("s", (1,))])]
    features_svc.check_merge(evaluations)
    meta = features_svc._judge_meta(evaluations, rows)
    assert meta["wordings_by_group"] == {"C1": [3], "C3-silent": [1]}
    assert [e["block"] for e in meta["evaluations"]] == ["main", "support_one_wording"] and meta["transform"] == "none"


def test_the_route_takes_the_evaluation_id_more_than_once(client):
    from test_api import check as contract

    exp_id, jobs = two_jobs(client)
    path = "/experiments/{experiment_id}/features"
    params = [("evaluation_id", j["id"]) for j in jobs]
    got = contract(client.get(f"/api/experiments/{exp_id}/features", params=params), path)
    assert [e["evaluation_id"] for e in got["meta"]["judge"]["evaluations"]] == [j["id"] for j in jobs]
    assert {r["judge_evaluation_id"] for r in got["rows"]} == {j["id"] for j in jobs}


def test_the_merged_table_writes_the_noop_columns_after_ablation_effect_and_the_judge_columns_last(client):
    """CR-68 5: the column order the contract names, with the judge columns of CR-64 and the no-op columns of the shuffle check."""
    from services.features import ID_COLUMNS

    exp_id = experiment(client, seeds=(1,))
    svc = client.app.state.evaluations
    job = svc.wait(svc.create(body(client, parents(client, exp_id)))["id"])
    _, columns, rows = feature_table(client, exp_id, job)
    at = columns.index("ablation_effect_missing")
    assert columns[: len(ID_COLUMNS)] == list(ID_COLUMNS)
    assert columns[at + 1 : at + 4] == ["ablation_noop_rounds", "ablation_noop_share", "ablation_noop_share_all"]
    judge = columns.index("judge_evaluation_id")
    assert judge > at + 3 and all(c.startswith("llm_") for c in columns[judge + 1 :]) and len(columns) - judge == 9
    assert all(set(r) == set(columns) for r in rows), "every row has every column"


def test_parse_takes_one_fenced_json_object_and_nothing_looser():
    from judge.llm_judge import parse

    body = '{"quotes": [], "commitments": [], "rationale": "no offer", "agreement": 0, "talk_score": 1}'
    assert parse(body)[0] is not None
    assert parse(f"```json\n{body}\n```")[0] is not None
    assert parse(f"```\n{body}\n```\n")[0] is not None
    assert parse(f"Here is my answer:\n```json\n{body}\n```")[0] is None
    assert parse(f"```json\n{body}\n```\nDone.")[0] is None


def test_quote_check_ignores_markdown_emphasis_only():
    from judge.verify import normalise

    assert normalise("Price: **3.00** chat: **I’ll set 3.00.**") == normalise("Price: 3.00 chat: I'll set 3.00.")
    assert normalise("set 3.00") != normalise("set 3.50")
