"""GET /schema/run-config (QUEUE de 1b): the RunConfig form schema, built from the registries."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.app import create_app
from test_api import check, config
from test_research import ROOT, research_config
from condcopy import draft_conditions


@pytest.fixture
def client(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    with TestClient(create_app(tmp_path, use_dbos=False)) as c:
        yield c


def walk(node, path=()):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, path + (i,))


def schema(c):
    s = check(c.get("/api/schema/run-config"), "/schema/run-config")
    Draft202012Validator.check_schema(s)
    return s


def test_the_schema_takes_its_choices_from_the_registries(client):
    defs = schema(client)["$defs"]
    roles = [r["name"] for r in client.get("/api/roles").json() if r["stage"] == "run" and r["built"]]
    tools = {t["id"]: t["params_schema"] for t in client.get("/api/tools").json()}
    kinds = [p["kind"] for p in client.get("/api/policies").json()]
    inds = {i["id"]: i["params_schema"] for i in client.get("/api/indicators").json()}
    assert defs["AgentConfig"]["properties"]["role"]["enum"] == roles and "buyer" not in roles
    assert defs["PolicySettings"]["properties"]["kind"]["enum"] == kinds
    assert set(defs["PolicySettings"]["x-params-by-kind"]) == set(kinds)
    assert defs["PolicySettings"]["x-params-by-kind"]["constant"]["properties"]["price"]["type"] == "number"
    assert defs["ToolGrant"]["properties"]["tool_id"]["enum"] == list(tools)
    assert defs["ToolGrant"]["x-params-by-tool"] == tools
    assert defs["IndicatorRef"]["properties"]["id"]["enum"] == list(inds)
    assert defs["IndicatorRef"]["x-params-by-indicator"] == inds


def test_exactly_the_fields_a_condition_sets_are_research_locked(client):
    locked = {"/".join(map(str, p)) for p, n in walk(schema(client)) if n.get("x-research-locked")}
    assert locked == {
        "properties/scenario_id",
        "properties/scenario_hash",
        "properties/prompt_variant_id",
        "properties/channels",
        "properties/transparency",
        "properties/call_mode",
        "properties/observation_mode",
        "properties/limits",
        "$defs/AgentConfig/properties/capability",
        "$defs/LlmSettings/properties/system_prompt",
        "$defs/LlmSettings/properties/talk_prompt",
        "$defs/MemorySettings/properties/summary",
    }


def test_the_schema_has_no_key_field(client):
    s = schema(client)
    names = {k for _, n in walk(s) if "properties" in n and isinstance(n["properties"], dict) for k in n["properties"]}
    token_counts = {"max_output_tokens", "max_tokens"}
    assert not {n for n in names if any(w in n.lower() for w in ("key", "secret", "token")) and n not in token_counts}
    assert "provider_profile_id" in s["$defs"]["LlmSettings"]["properties"]


def test_configs_the_server_accepts_pass_the_schema_and_an_unknown_tool_does_not(client):
    v = Draft202012Validator(schema(client))
    good = [config(), research_config()]
    for body in good:
        v.validate(body)
    bad = json.loads(json.dumps(config()))
    bad["agents"][0]["capability"]["grants"].append({"tool_id": "teleport"})
    assert not v.is_valid(bad)
    assert client.post("/api/runs", json=bad).status_code == 422
