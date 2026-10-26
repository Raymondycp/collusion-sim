"""Unit 1c: settings from the web UI (DECISIONS-11 §1): PUT /defaults, research files, condition files, freeze inputs."""

import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from api.app import create_app
from test_api import check
from test_judge import RUBRIC_TEXT as RUBRIC  # a valid item set: stage conditions parses it
from test_judge import write_models
from test_profiles import KEY, MemoryKeyring, profile_body
from test_research import ROOT
from condcopy import draft_conditions

RULE = "rule: corr_lag > 0.4\n"


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    (tmp_path / ".env").write_text("SIM_DEFAULT_SELLER_MODEL=env-seller\nSIM_DEFAULT_JUDGE_MODEL=env-judge\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def client(root):
    with TestClient(create_app(root, use_dbos=False, key_backend=MemoryKeyring())) as c:
        yield c


def put(client, path, body, contract_path=None):
    return check(client.put(f"/api{path}", json=body), contract_path or path, "put")


# Defaults


def test_saved_defaults_replace_the_env_values_role_by_role_and_survive_a_restart(root):
    with TestClient(create_app(root, use_dbos=False, key_backend=MemoryKeyring())) as client:
        pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
        before = check(client.get("/api/defaults"), "/defaults")
        assert before["seller"]["model"] == "env-seller" and before["judge"]["model"] == "env-judge"
        body = {"seller": {"profile_id": pid, "model": "m-small", "agents": {"B": {"profile_id": pid, "model": "m-big"}}}}
        after = put(client, "/defaults", body)
        assert after["seller"] == {"profile_id": pid, "model": "m-small", "agents": {"B": {"profile_id": pid, "model": "m-big"}}}
        assert after["judge"]["model"] == "env-judge"  # a role left out keeps its .env values
        assert KEY not in (root / "data" / "defaults.json").read_text(encoding="utf-8")
    with TestClient(create_app(root, use_dbos=False, key_backend=MemoryKeyring())) as again:  # the first server has stopped
        assert again.get("/api/defaults").json()["seller"]["model"] == "m-small"


@pytest.mark.parametrize(
    ("body", "path"),
    [
        ({"seller": {"profile_id": "prof_missing", "model": "m"}}, "seller/profile_id"),
        ({"judge": {"agents": {"A": {"profile_id": KEY}}}}, "judge/agents/A/profile_id"),
    ],
)
def test_a_default_must_name_an_existing_profile(client, root, body, path):
    err = put(client, "/defaults", body)
    assert err["code"] == "validation_error" and [e["path"] for e in err["errors"]] == [path]
    assert KEY not in err["message"] and not (root / "data" / "defaults.json").exists()


@pytest.mark.parametrize("body", [{"sellers": {}}, {"seller": {"agents": {"a": {}}}}, {"seller": {"model": ""}}])
def test_a_defaults_body_outside_the_shape_is_refused(client, body):
    assert client.put("/api/defaults", json=body).status_code == 422


# Research files


@pytest.mark.parametrize(("name", "text"), [("decision-rule", RULE), ("judge-rubric", RUBRIC)])
def test_a_research_file_is_saved_as_text_and_its_hash_is_the_freeze_input(client, root, name, text):
    empty = check(client.get(f"/api/research/{name}"), "/research/{name}")
    assert empty == {"name": f"conditions/{name}.yaml", "text": None, "hash": None, "updated_at": None}
    key = name.replace("-", "_")
    assert check(client.get("/api/freeze"), "/freeze")["inputs"][key] is None
    saved = put(client, f"/research/{name}", {"text": text.replace("\n", "\r\n")}, "/research/{name}")
    assert saved["text"] == text and saved["hash"].startswith("sha256:") and saved["updated_at"]
    assert (root / "conditions" / f"{name}.yaml").read_bytes() == text.encode()
    assert check(client.get("/api/freeze"), "/freeze")["inputs"][key] == saved["hash"]
    assert saved.pop("invalidated_stage") is None and client.get(f"/api/research/{name}").json() == saved


def test_a_research_file_must_be_yaml_and_is_frozen_with_the_freeze(client, root):
    err = put(client, "/research/decision-rule", {"text": "rule: [unclosed"}, "/research/{name}")
    assert err["code"] == "validation_error" and not (root / "conditions" / "decision-rule.yaml").exists()
    assert client.get("/api/research/other").status_code == 422
    put(client, "/research/decision-rule", {"text": RULE}, "/research/{name}")
    put(client, "/research/judge-rubric", {"text": RUBRIC}, "/research/{name}")
    write_models(root / "conditions")
    check(client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"}), "/freeze", "post")
    err = put(client, "/research/judge-rubric", {"text": "items: [offer]\n"}, "/research/{name}")
    assert (err["status"], err["code"]) == (409, "research_frozen")  # the rubric freezes with stage conditions (L1)
    assert (root / "conditions" / "judge-rubric.yaml").read_text(encoding="utf-8") == RUBRIC
    saved = put(client, "/research/decision-rule", {"text": RULE}, "/research/{name}")  # dev tunes the rule before its stage
    assert saved["invalidated_stage"] is None


# Condition files


def test_a_draft_condition_file_is_read_and_saved_as_text(client, root):
    path = root / "conditions" / "C1.yaml"
    got = check(client.get("/api/conditions/C1/file"), "/conditions/{condition_id}/file")
    assert got["name"] == "conditions/C1.yaml" and got["text"] == path.read_text(encoding="utf-8")
    text = got["text"].replace("name: Coordinated harmful action", "name: Coordinated harmful action (edited)")
    saved = put(client, "/conditions/C1/file", {"text": text}, "/conditions/{condition_id}/file")
    assert saved["text"] == text and saved["hash"] != got["hash"] and path.read_text(encoding="utf-8") == text
    assert client.get("/api/conditions/C1").json()["name"] == "Coordinated harmful action (edited)"


def _with(text: str, **changes) -> str:
    data = yaml.safe_load(text)
    data.update(changes)
    return yaml.safe_dump(data, sort_keys=False)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "C2a"},
        {"status": "frozen", "frozen_hash": "sha256:" + "0" * 64},
        {"labels": {"legal": "maybe", "harm": "yes"}},
        {"group": None},
    ],
)
def test_a_condition_file_that_is_not_a_valid_draft_is_refused_and_not_written(client, root, change):
    path = root / "conditions" / "C1.yaml"
    before = path.read_text(encoding="utf-8")
    err = put(client, "/conditions/C1/file", {"text": _with(before, **change)}, "/conditions/{condition_id}/file")
    assert err["code"] == "validation_error" and path.read_text(encoding="utf-8") == before


def test_a_condition_with_a_prompt_path_that_does_not_resolve_is_refused(client, root):
    before = (root / "conditions" / "C1.yaml").read_text(encoding="utf-8")
    sellers = yaml.safe_load(before)["sellers"]
    first = next(iter(sellers))
    sellers[first]["pricing"] = "no/such/prompt"
    err = put(client, "/conditions/C1/file", {"text": _with(before, sellers=sellers)}, "/conditions/{condition_id}/file")
    assert err["code"] == "validation_error"


def test_an_unknown_condition_is_404_and_a_frozen_one_is_409(client, root):
    assert check(client.get("/api/conditions/C9/file"), "/conditions/{condition_id}/file")["code"] == "not_found"
    assert put(client, "/conditions/C9/file", {"text": "id: C9\n"}, "/conditions/{condition_id}/file")["code"] == "not_found"
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC, encoding="utf-8")
    write_models(root / "conditions")
    check(client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"}), "/freeze", "post")
    text = client.get("/api/conditions/C1/file").json()["text"]
    err = put(client, "/conditions/C1/file", {"text": text}, "/conditions/{condition_id}/file")
    assert (err["status"], err["code"]) == (409, "condition_frozen")


@pytest.mark.parametrize("name", ["seeds", "decision-rule", "judge-rubric", "prompts"])
def test_a_file_in_conditions_that_is_not_a_condition_is_not_found(client, root, name):
    (root / "conditions" / "decision-rule.yaml").write_text(RULE, encoding="utf-8")
    (root / "conditions" / "judge-rubric.yaml").write_text(RUBRIC, encoding="utf-8")
    write_models(root / "conditions")
    before = {p.name: p.read_bytes() for p in (root / "conditions").iterdir()}
    for r in (client.get(f"/api/conditions/{name}"), client.get(f"/api/conditions/{name}/file")):
        assert (r.status_code, r.json()["code"]) == (404, "not_found")
    text = f"id: {name}\nname: x\nstatus: draft\nfrozen_hash: null\n"
    assert put(client, f"/conditions/{name}/file", {"text": text}, "/conditions/{condition_id}/file")["code"] == "not_found"
    assert before == {p.name: p.read_bytes() for p in (root / "conditions").iterdir()}


def test_freeze_inputs_list_every_input_with_its_hash_now(client):
    inputs = check(client.get("/api/freeze"), "/freeze")["inputs"]
    assert inputs == client.app.state.research.current_hashes() | {"decision_rule": None, "judge_rubric": None, "models": None}
    assert {"seeds", "features", "condition:C1"} <= set(inputs)
