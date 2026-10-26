import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.app import create_app
from fakellm import FakeLLM, cooperator
from services import seeds as seeds_svc
from services.catalog import Catalog, Paths
from services.research import ConditionFile, Research, resolve
from test_api import SPEC, check
from test_profiles import KEY, MemoryKeyring, no_key_anywhere, profile_body
from test_prompts import FORBIDDEN
from condcopy import draft_conditions

ROOT = Path(__file__).parents[1]
PROMPTS = yaml.safe_load((ROOT / "conditions" / "prompts.draft.yaml").read_text(encoding="utf-8"))

# Plan §11, "Labels for each group under both definitions" (DECISIONS-10 §1).
LABELS = {
    "C1": ("yes", "yes"),
    "C1-public": ("yes", "yes"),
    "C1-public-private": ("yes", "yes"),
    "C2a": ("no", "no"),
    "C2b": ("no", "no"),
    "C3-accept": ("yes", "no"),
    "C3-silent": ("no", "no"),
}


@pytest.fixture
def research(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    catalog = Catalog(Paths(tmp_path))
    yield Research(tmp_path / "conditions", catalog)
    catalog.close()


@pytest.fixture
def client(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    with TestClient(create_app(tmp_path, use_dbos=False)) as c:
        yield c


def research_config(condition="C1", seeds=(1,), split="pilot", policies=None, **kw):
    policies = policies or {"A": ("grim_trigger", {"target": 10.5}), "B": ("constant", {"price": 10.5})}
    agents = [{"id": s, "role": "seller", "policy": {"kind": k, "params": p}} for s, (k, p) in policies.items()]
    body = {
        "kind": "research",
        "condition_id": condition,
        "split": split,
        "seeds": list(seeds),
        "agents": agents,
        "clock": {"mode": "sync"},
        "budget": {"max_usd": 1.0},
    }
    return {**body, **kw}


def started(client, body) -> list[str]:
    out = check(client.post("/api/runs", json=body), "/runs", "post")
    for rid in out["run_ids"]:
        client.app.state.svc.wait(rid, timeout=30)
    return out["run_ids"]


def freeze_condition(client, root: Path, condition_id: str) -> None:
    """The conditions stage of the freeze (DECISIONS-11 §2.5): it freezes every condition and the rubric, and opens dev."""
    rubric = root / "conditions" / "judge-rubric.yaml"
    if not rubric.exists():
        from test_judge import RUBRIC_TEXT  # a valid item set: stage conditions parses it (CR-37 m1)

        rubric.write_text(RUBRIC_TEXT, encoding="utf-8")
    if not (root / "conditions" / "models.yaml").exists():
        from test_judge import write_models

        write_models(root / "conditions")
    r = client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"})
    assert r.status_code == 200, r.text


# Condition files


def test_the_condition_files_carry_the_plan_label_table(research):
    conditions = {c.id: c.file for c in research.conditions()}
    assert {cid: (f.labels.legal, f.labels.harm) for cid, f in conditions.items()} == LABELS
    assert {cid: f.sub_condition for cid, f in conditions.items() if f.group == "C3"} == {
        "C3-accept": "responder_accepts",
        "C3-silent": "responder_silent",
    }
    assert {cid for cid, f in conditions.items() if f.tag == "required"} == {"C1", "C2a", "C3-accept"}  # DECISIONS-16
    assert all(f.status == "draft" and f.transparency == "rival_last_round" and f.market_preset == "base" for f in conditions.values())


def test_no_resolved_prompt_names_a_group_a_label_or_the_study(research):
    for c in research.conditions():
        assert [v.id for v in c.prompt_variants] == ["v1", "v2"]
        for v in c.prompt_variants:
            for sid, p in v.sellers.items():
                for text in (p.system_prompt, p.talk_prompt):
                    assert text is None or not FORBIDDEN.search(text), (c.id, v.id, sid)


def test_only_c3_isolates_pricing_and_c3_accept_talks_with_the_c1_talk_prompt(research):
    """DECISIONS-27 §3: both C3-accept talk calls use groups/C1/talk; the sub-condition stays responder_accepts."""
    by_id = {c.id: c for c in research.conditions()}
    assert {cid for cid, c in by_id.items() if c.file.c3_isolation} == {"C3-accept", "C3-silent"}
    for cid in ("C3-accept", "C3-silent"):
        for v in by_id[cid].prompt_variants:
            assert {s.system_prompt for s in v.sellers.values()} == {PROMPTS["base"][v.id].strip()}
    c1_talk = PROMPTS["groups"]["C1"]["talk"]
    for v, c1 in zip(by_id["C3-accept"].prompt_variants, by_id["C1"].prompt_variants, strict=True):
        assert {s: p.talk_prompt for s, p in v.sellers.items()} == {"A": c1_talk[v.id].strip(), "B": c1_talk[v.id].strip()}
        assert {s: p.talk_prompt for s, p in c1.sellers.items()} == {s: p.talk_prompt for s, p in v.sellers.items()}, "C3 talk = C1 talk"
    assert by_id["C3-accept"].file.sub_condition == "responder_accepts"
    proposer = PROMPTS["groups"]["C3"]["talk"]["proposer"]
    assert [v.sellers["A"].talk_prompt for v in by_id["C3-silent"].prompt_variants] == [proposer[k].strip() for k in ("v1", "v2")]
    silent_b = by_id["C3-silent"].file.sellers["B"]
    assert silent_b.talk is None and "post" not in {g.tool_id for g in silent_b.grants}
    assert all(not s.talk for cid in ("C2a", "C2b") for s in by_id[cid].file.sellers.values())


def _c1():
    d = yaml.safe_load((ROOT / "conditions" / "C1.yaml").read_text(encoding="utf-8"))
    return d | {"status": "draft", "frozen_hash": None}  # the repo file is frozen since 24 Oct


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(transparency="opaque"),
        lambda d: d.update(c3_isolation=True, call_mode="one_call"),
        lambda d: d.update(channels={"public": False, "private_pairs": False}),
        lambda d: d.update(sub_condition="responder_accepts"),
        lambda d: d["sellers"]["A"].update(talk=None),
        lambda d: d.update(call_mode="one_call") or d["sellers"]["A"].update(talk="groups/C3/talk/proposer"),
        lambda d: d.update(labels={"legal": "maybe", "harm": "yes"}),
        lambda d: d.update(status="frozen"),
        lambda d: d.update(frozen_hash="sha256:" + "0" * 64),
    ],
)
def test_an_inconsistent_condition_file_is_refused(change):
    d = _c1()
    d["sellers"] = {k: dict(v) for k, v in d["sellers"].items()}
    change(d)
    with pytest.raises(ValueError):
        ConditionFile.model_validate(d)


def test_the_condition_hash_covers_the_prompt_text_but_not_the_status():
    file = ConditionFile.model_validate(_c1())
    base = resolve(file, PROMPTS).hash
    assert resolve(file.model_copy(update={"status": "frozen", "frozen_hash": base}), PROMPTS).hash == base
    edited = json.loads(json.dumps(PROMPTS))
    edited["groups"]["C1"]["pricing"]["v2"] += " Extra."
    assert resolve(file, edited).hash != base
    d = _c1()
    d["sellers"] = {
        "A": d["sellers"]["A"],
        "B": {**d["sellers"]["B"], "grants": d["sellers"]["B"]["grants"][:2] + d["sellers"]["B"]["grants"][3:]},
    }
    d["transparency"] = "rival_last_round"
    with pytest.raises(ValueError):
        ConditionFile.model_validate(d)  # B without get_rival_prices
    d["sellers"]["B"]["grants"] = [
        {**g, "params": {"depth": 2}} if g["tool_id"] == "get_rival_prices" else g for g in _c1()["sellers"]["B"]["grants"]
    ]
    assert resolve(ConditionFile.model_validate(d), PROMPTS).hash != base


# Seed map


def test_the_seed_map_has_disjoint_splits_and_every_seed_scenario_regenerates_with_its_hash(research):
    m = research.seed_map()
    assert (len(m.split("pilot")), len(m.split("dev")), len(m.split("test"))) == (3, 10, 10)
    assert m.generator.inputs == seeds_svc.BOUNDS and m.generator.id == "random"
    for e in m.seeds:
        scenario = research.catalog.scenario(e.scenario_id)
        assert scenario.status == "frozen" and scenario.hash == e.scenario_hash
        assert seeds_svc.generate(e.seed).content_hash() == e.scenario_hash, e.seed
        assert seeds_svc.grid_problems(scenario) == []
        assert scenario.horizon.max_rounds >= 25  # the probe at round 20 runs 5 rounds (plan §11)
        cost = [ev for ev in scenario.events if any(sh.path == "/c" for sh in ev.shocks)]
        assert cost, e.seed  # C2a: a common cost shock
    news = {split: [e.shock_news for e in m.seeds if e.split == split] for split in ("pilot", "dev", "test")}
    assert {k: (v.count("public"), v.count("private")) for k, v in news.items()} == {"pilot": (2, 1), "dev": (5, 5), "test": (5, 5)}


def test_every_seed_shows_its_demand_and_cost_news_as_its_shock_news_label_says(research):
    """Lead decisions (CR-8, CR-12 m3): shock_news is shared public information in general. public: both sellers see
    every shock; private: one fixed informed seller sees every shock, alternating A, B over the private seeds of a split."""
    seen = set()
    m = research.seed_map()
    for e in m.seeds:
        assert (e.informed_seller is None) == (e.shock_news == "public"), e.seed
        for ev in research.catalog.scenario(e.scenario_id).events:
            for sh in ev.shocks:
                seen.add(sh.path)
                assert ev.visible == (["A", "B"] if e.shock_news == "public" else [e.informed_seller]), (e.seed, sh.path)
    assert seen == {"/a", "/c"}
    for split in ("pilot", "dev", "test"):
        informed = [e.informed_seller for e in m.seeds if e.split == split and e.shock_news == "private"]
        assert informed == ["AB"[i % 2] for i in range(len(informed))], split


def test_making_the_seed_map_again_changes_nothing(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    (tmp_path / "conditions").mkdir()
    before = {p.name: p.read_bytes() for p in (tmp_path / "scenarios").glob("scn_s*.json")}
    seeds_svc.make(tmp_path, approved_by="test")
    assert {p.name: p.read_bytes() for p in (tmp_path / "scenarios").glob("scn_s*.json")} == before
    assert (tmp_path / "conditions" / "seeds.yaml").read_bytes() == (ROOT / "conditions" / "seeds.yaml").read_bytes()


# API: routes


def test_condition_seed_and_freeze_routes_match_the_contract(client):
    listed = check(client.get("/api/conditions"), "/conditions")
    assert [c["id"] for c in listed] == sorted(LABELS, key=lambda c: f"{c}.yaml")  # file name order
    c1 = check(client.get("/api/conditions/C1"), "/conditions/{condition_id}")
    assert c1["labels"] == {"legal": "yes", "harm": "yes"} and set(c1["capabilities"]) == {"A", "B"}
    for bad in ("nope", "..", "C1.yaml"):
        assert check(client.get(f"/api/conditions/{bad}"), "/conditions/{condition_id}")["code"] == "not_found"
    seeds = check(client.get("/api/seeds"), "/seeds")
    assert seeds["pilot"] == [1, 2, 3] and seeds["test_locked"] is True
    by_seed = {e["seed"]: e for e in seeds["seeds"]}  # CR-11: the design balance is visible through the API
    assert set(by_seed) == {*seeds["pilot"], *seeds["dev"], *seeds["test"]}
    assert by_seed[3] == {
        "seed": 3,
        "split": "pilot",
        "shock_news": "private",
        "informed_seller": "A",
        "scenario_id": "scn_s3",
        "scenario_hash": client.app.state.research.seed_map().entry(3).scenario_hash,
    }
    assert by_seed[1]["shock_news"] == "public" and by_seed[1]["informed_seller"] is None
    status = check(client.get("/api/freeze"), "/freeze")
    assert {k: status[k] for k in ("frozen", "frozen_at", "valid", "problems")} == {
        "frozen": False,
        "frozen_at": None,
        "valid": False,
        "problems": ["frozen.json not found"],
    }
    assert {k: v["frozen"] for k, v in status["stages"].items()} == {"conditions": False, "rule": False}
    assert status["inputs"]["decision_rule"] is None and "condition:C1" in status["inputs"]


def test_freeze_status_is_valid_only_while_every_hash_matches(client, tmp_path):
    research = client.app.state.research
    (tmp_path / "conditions" / "decision-rule.yaml").write_text("rule: x\n", encoding="utf-8")
    freeze_condition(client, tmp_path, "C1")
    research.freeze("rule", "p2", 0)
    assert check(client.get("/api/freeze"), "/freeze")["valid"] is True
    prompts = tmp_path / "conditions" / "prompts.draft.yaml"
    prompts.write_text(prompts.read_text(encoding="utf-8").replace("free to coordinate", "free to talk"), encoding="utf-8")
    status = check(client.get("/api/freeze"), "/freeze")
    assert status["valid"] is False
    assert status["problems"] == [  # C1-public, C1-public-private and C3-accept use the C1 prompts: their hashes change too
        "condition:C1 changed since the freeze",
        "condition:C1-public changed since the freeze",
        "condition:C1-public-private changed since the freeze",
        "condition:C3-accept changed since the freeze",
        "condition:C1-public-private is not frozen with its hash",
        "condition:C1-public is not frozen with its hash",
        "condition:C1 is not frozen with its hash",
        "condition:C3-accept is not frozen with its hash",
    ]


# API: research runs


def test_a_pilot_research_run_takes_its_scenario_labels_and_grants_from_the_files(client):
    run_ids = started(client, research_config(seeds=(1, 2)))
    cond = client.get("/api/conditions/C1").json()
    seeds_hash = client.get("/api/seeds").json()["hash"]
    for rid, seed, variant in zip(run_ids, (1, 2), ("v2", "v1")):
        detail = check(client.get(f"/api/runs/{rid}"), "/runs/{run_id}")
        assert detail["status"] == "completed" and detail["config"]["prompt_variant_id"] == variant
        header = client.get(f"/api/runs/{rid}/events", params={"limit": 1}).json()["events"][0]["header"]
        assert header["kind"] == "research" and header["split"] == "pilot" and header["seed"] == seed
        assert (header["condition_id"], header["condition_hash"], header["seeds_hash"]) == ("C1", cond["hash"], seeds_hash)
        assert header["labels"] == {"legal": "yes", "harm": "yes"} and header["label_source"] == "condition_config"
        assert header["scenario"]["id"] == f"scn_s{seed}" and [c["id"] for c in header["channels"]] == ["private_A_B"]
        assert {a["id"]: a["capability"] for a in header["agents"] if a["role"] == "seller"} == cond["capabilities"]
    entries = {e["run_id"]: e for e in client.get("/api/ledger").json()["items"]}
    assert entries[run_ids[0]]["config_hash"] == entries[run_ids[1]]["config_hash"]


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("scenario_id", "scn_s1", "scenario_id"),
        ("call_mode", "talk_and_act", "call_mode"),
        ("channels", {"private_pairs": True}, "channels"),
        ("transparency", "opaque", "transparency"),
        ("prompt_variant_id", "v1", "prompt_variant_id"),
    ],
)
def test_a_research_config_cannot_set_what_the_condition_sets(client, field, value, path):
    body = check(client.post("/api/runs", json=research_config(**{field: value})), "/runs", "post")
    assert body["status"] == 422 and body["code"] == "research_field_locked" and body["errors"][0]["path"] == path


def test_a_research_config_cannot_set_grants_or_prompts(client):
    body = research_config()
    body["agents"][0]["capability"] = {"grants": []}
    body["agents"][1]["policy"] = {"kind": "llm", "llm": {"provider_profile_id": "p", "model": "m", "system_prompt": "x"}}
    out = check(client.post("/api/runs", json=body), "/runs", "post")
    assert {e["path"] for e in out["errors"]} == {"agents/0/capability", "agents/1/policy/llm/system_prompt"}


@pytest.mark.parametrize(
    ("kw", "status", "code"),
    [
        ({"split": "test", "seeds": [201]}, 409, "test_split_locked"),
        ({"split": "dev", "seeds": [101]}, 409, "condition_not_frozen"),
        ({"seeds": [101]}, 422, "seed_not_in_split"),
        ({"seeds": [999]}, 422, "seed_not_in_split"),
        ({"split": "exploratory"}, 422, "validation_error"),
        ({"condition_id": "C9"}, 422, "validation_error"),
        ({"memory": {"summary": "llm"}}, 422, "research_field_locked"),
    ],
)
def test_split_and_seed_rules(client, kw, status, code):
    body = check(client.post("/api/runs", json=research_config(**kw)), "/runs", "post")
    assert (body["status"], body["code"]) == (status, code)


def test_dev_seeds_run_once_the_condition_is_frozen(client, tmp_path):
    freeze_condition(client, tmp_path, "C1")
    assert client.get("/api/conditions/C1").json()["status"] == "frozen"
    (rid,) = started(client, research_config(split="dev", seeds=(101,)))
    assert client.get(f"/api/runs/{rid}").json()["status"] == "completed"


def test_a_frozen_condition_that_changed_after_the_freeze_is_refused(client, tmp_path):
    freeze_condition(client, tmp_path, "C1")
    prompts = tmp_path / "conditions" / "prompts.draft.yaml"
    prompts.write_text(prompts.read_text(encoding="utf-8").replace("free to coordinate", "free to talk"), encoding="utf-8")
    for split, seed in (("dev", 101), ("pilot", 1)):
        body = check(client.post("/api/runs", json=research_config(split=split, seeds=(seed,))), "/runs", "post")
        assert (body["status"], body["code"]) == (409, "hash_mismatch")


def test_a_seed_map_hash_that_does_not_match_the_scenario_file_is_refused(client, tmp_path):
    path = tmp_path / "conditions" / "seeds.yaml"
    m = client.app.state.research.seed_map()
    path.write_text(path.read_text(encoding="utf-8").replace(m.entry(2).scenario_hash, "sha256:" + "0" * 64), encoding="utf-8")
    assert check(client.post("/api/runs", json=research_config(seeds=(2,))), "/runs", "post")["code"] == "hash_mismatch"


@pytest.mark.parametrize("field", [{"condition_id": "C1"}, {"prompt_variant_id": "v1"}])
def test_an_exploratory_config_refuses_research_only_fields(client, field):
    from test_api import config

    body = check(client.post("/api/runs", json=config(**field)), "/runs", "post")
    assert (body["status"], body["code"]) == (422, "validation_error") and client.get("/api/runs").json()["items"] == []


def test_no_run_is_stored_when_one_seed_of_the_batch_is_refused(client):
    body = check(client.post("/api/runs", json=research_config(seeds=(1, 101))), "/runs", "post")
    assert body["code"] == "seed_not_in_split" and client.get("/api/runs").json()["items"] == []


@pytest.mark.parametrize(
    ("condition", "policies", "ok"),
    [
        ("C2a", {"A": ("constant", {"price": 5.75}), "B": ("constant", {"price": 5.75})}, True),
        ("C2a", {"A": ("best_response", {}), "B": ("constant", {"price": 5.75})}, False),
        ("C2b", {"A": ("constant", {"price": 5.75}), "B": ("constant", {"price": 5.75})}, False),
        ("C2b", {"A": ("best_response", {}), "B": ("constant", {"price": 5.75})}, True),
    ],
)
def test_c2a_needs_one_model_and_c2b_a_model_family_per_seller(client, condition, policies, ok):
    resp = client.post("/api/runs", json=research_config(condition, policies=policies))
    assert resp.status_code == (202 if ok else 422), resp.json()
    if ok:
        client.app.state.svc.wait(resp.json()["run_ids"][0], timeout=30)


def test_the_demand_model_rule_still_applies_to_research_runs(client, monkeypatch):
    from dataclasses import replace

    from simcore.demand import DEMAND
    from simcore.demand.builtin.linear import LINEAR

    monkeypatch.setitem(DEMAND._specs, ("linear", "1"), replace(LINEAR, references=frozenset()))
    body = check(client.post("/api/runs", json=research_config()), "/runs", "post")
    assert body["code"] == "demand_not_research_allowed"


# LLM agents: the condition's prompts reach the calls (fake endpoint, no paid call)


@pytest.fixture
def llm_client(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    fake = FakeLLM(cooperator(10.5))
    with TestClient(create_app(tmp_path, use_dbos=False, key_backend=MemoryKeyring(), llm_transport=fake.transport)) as c:
        c.fake = fake
        yield c


def test_c3_talk_calls_get_the_talk_prompt_and_pricing_calls_never_see_the_channel(llm_client, tmp_path):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    llm = {"kind": "llm", "llm": {"provider_profile_id": pid, "model": "m-small", "model_family": "fake", "temperature": 0.2}}
    body = research_config("C3-accept", seeds=(1,))
    for a in body["agents"]:
        a["policy"] = llm
    (rid,) = started(c, body)
    assert c.get(f"/api/runs/{rid}").json()["status"] == "completed"
    v2 = {v["id"]: v for v in c.get("/api/conditions/C3-accept").json()["prompt_variants"]}["v2"]["sellers"]
    talk = [b for b in c.fake.bodies if "set_price" not in [t["function"]["name"] for t in b.get("tools", [])]]
    act = [b for b in c.fake.bodies if "set_price" in [t["function"]["name"] for t in b.get("tools", [])]]
    assert talk and act
    prompts = [b["messages"][0]["content"].split("\n\nMarket facts: ") for b in c.fake.bodies]
    assert {len(x) for x in prompts} == {2}, "every system prompt ends with the market facts (DECISIONS-22 entry 5.1)"
    assert {x[1].endswith(" Your sales fall as your price rises and rise as the rival's price rises.") for x in prompts} == {True}, (
        "the demand direction, in the talk and the act prompt of both sellers (DECISIONS-25 entry 1)"
    )
    assert {b["messages"][0]["content"].split("\n\nMarket facts: ")[0] for b in talk} == {v2["A"]["talk_prompt"], v2["B"]["talk_prompt"]}
    assert {b["messages"][0]["content"].split("\n\nMarket facts: ")[0] for b in act} == {v2["A"]["system_prompt"]}
    assert all("read_channel" not in [t["function"]["name"] for t in b["tools"]] for b in act)
    header = c.get(f"/api/runs/{rid}/events", params={"limit": 1}).json()["events"][0]["header"]
    assert len({a["prompt_hash"] for a in header["agents"]}) == 1, "A and B have the same C1 talk prompt (DECISIONS-27 §3)"
    no_key_anywhere(tmp_path, json.dumps(header))
    assert KEY not in json.dumps(c.get(f"/api/runs/{rid}").json())


def test_the_contract_lists_every_new_error_code():
    codes = set(SPEC["components"]["schemas"]["Problem"]["properties"]["code"]["enum"])
    assert {
        "test_split_locked",
        "condition_not_frozen",
        "seed_not_in_split",
        "seeds_missing",
        "condition_invalid",
        "seeds_invalid",
    } <= codes
