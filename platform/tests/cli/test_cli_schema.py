"""schema run-config: the API body unchanged, a readable summary, one part by choice, and --check FILE (local validation, no write)."""

import json
import shutil
from importlib.metadata import requires

import httpx2
import pytest
from click.testing import CliRunner
from conftest import ROOT, Result, scripted_config
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.app import create_app
from simcli.main import cli
from condcopy import draft_conditions

PARTS = {
    "policy": ("PolicySettings", "x-params-by-kind"),
    "tool": ("ToolGrant", "x-params-by-tool"),
    "indicator": ("IndicatorRef", "x-params-by-indicator"),
}


def run(http, *args: str) -> Result:
    r = CliRunner().invoke(cli, list(args), obj={"http": http}, catch_exceptions=False)
    return Result(r.exit_code, r.stdout, r.stderr)


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    root = tmp_path_factory.mktemp("schemaroot")
    shutil.copytree(ROOT / "scenarios", root / "scenarios")
    draft_conditions(root / "conditions")
    with TestClient(create_app(root, use_dbos=False), base_url="http://testserver/api") as http:
        yield http, http.get("schema/run-config").json()


def locked(body: dict) -> set[str]:
    out = {f"RunConfig.{k}" for k, v in body["properties"].items() if v.get("x-research-locked")}
    return out | {f"{n}.{k}" for n, d in body["$defs"].items() for k, v in d.get("properties", {}).items() if v.get("x-research-locked")}


def test_json_is_the_api_body_and_a_valid_2020_12_schema(app):
    http, body = app
    r = run(http, "schema", "run-config", "--json")
    assert r.exit_code == 0 and r.json == body
    Draft202012Validator.check_schema(r.json)


@pytest.mark.parametrize("path", [None, ROOT / "docs" / "team" / "cli-examples" / "scripted-run.json"], ids=["test config", "team example"])
def test_known_good_configs_pass_the_schema(app, path):
    _, body = app
    config = scripted_config() if path is None else json.loads(path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(body).iter_errors(config)) == []


def test_summary_has_every_field_its_lock_and_the_choices(app):
    http, body = app
    out = run(http, "schema", "run-config").stdout
    lines = {line.split()[0]: line.split() for line in out.splitlines() if line.split()}
    for name, node in body["properties"].items():
        assert lines[name][-1] == str(bool(node.get("x-research-locked"))) and lines[name][-2] == str(name in body["required"])
    assert locked(body) and all(f"\n{p}\n" in out + "\n" for p in locked(body))
    shown = {words[0]: " ".join(words[1:]).split(", ") for words in lines.values() if words[0] in ("role", *PARTS)}
    assert shown["role"] == body["$defs"]["AgentConfig"]["properties"]["role"]["enum"]
    for option, (name, key) in PARTS.items():
        assert shown[option] == list(body["$defs"][name][key])


@pytest.mark.parametrize("option", PARTS)
def test_each_choice_prints_its_params_schema(app, option):
    http, body = app
    name, key = PARTS[option]
    for choice, params in body["$defs"][name][key].items():
        r = run(http, "schema", "run-config", f"--{option}", choice, "--json")
        assert r.exit_code == 0 and r.json == params, choice
        assert run(http, "schema", "run-config", f"--{option}", choice).exit_code == 0


def test_def_prints_one_entry(app):
    http, body = app
    r = run(http, "schema", "run-config", "--def", "LlmSettings", "--json")
    assert r.exit_code == 0 and r.json == body["$defs"]["LlmSettings"]
    human = run(http, "schema", "run-config", "--def", "LlmSettings").stdout
    assert next(x for x in human.splitlines() if x.startswith("system_prompt")).split()[-1] == "True"


@pytest.mark.parametrize("args", [["--policy", "nope"], ["--def", "Nope"], ["--tool", "nope"]])
def test_an_unknown_choice_is_a_usage_error_with_the_choices(app, args):
    http, body = app
    r = run(http, "schema", "run-config", *args, "--json")
    assert r.exit_code == 2 and r.json["code"] == "cli_usage" and "Choices:" in r.json["message"]


def test_two_parts_at_once_is_a_usage_error(app):
    http, _ = app
    assert run(http, "schema", "run-config", "--policy", "llm", "--tool", "post").exit_code == 2


def test_it_sends_one_get_only():
    sent = []
    reply = httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(200, json={"properties": {}, "$defs": {}}))
    run(httpx2.Client(base_url="http://fake/api", transport=reply), "schema", "run-config", "--json")
    assert [(r.method, r.url.path) for r in sent] == [("GET", "/api/schema/run-config")]


def config_file(tmp_path, body, name="config.json") -> str:
    path = tmp_path / name
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return str(path)


def research(**changes) -> dict:
    agents = [{k: v for k, v in a.items() if k != "capability"} for a in scripted_config()["agents"]]
    base = {"kind": "research", "condition_id": "C1", "split": "pilot", "seeds": [1], "agents": agents, "clock": {"mode": "sync"}}
    return {**base, "budget": {"max_usd": 1.0}, **changes}


@pytest.mark.parametrize(
    "body", [scripted_config(), research(), research(memory={"summary": None})], ids=["exploratory", "research", "memory"]
)
def test_check_passes_a_good_config_and_the_server_agrees(app, tmp_path, body):
    http, _ = app
    r = run(http, "schema", "run-config", "--check", config_file(tmp_path, body), "--json")
    assert r.exit_code == 0 and r.json == {"ok": True, "path": config_file(tmp_path, body), "kind": body["kind"], "errors": []}
    assert http.post("runs", json=body).status_code == 202


def test_check_gives_each_schema_problem_with_its_path(app, tmp_path):
    http, _ = app
    body = scripted_config()
    body["agents"][0]["policy"]["kind"] = "nope"
    del body["seeds"]
    r = run(http, "schema", "run-config", "--check", config_file(tmp_path, body), "--json")
    assert r.exit_code == 1 and r.json["code"] == "config_invalid"
    paths = {e["path"]: e["message"] for e in r.json["errors"]}
    assert "'seeds' is a required property" in paths["(root)"] and "agents/0/policy/kind" in paths


def test_check_finds_the_same_locked_fields_as_the_server(app, tmp_path):
    http, _ = app
    body = research(scenario_id="scn_s1", call_mode="one_call")
    body["agents"][0]["capability"] = scripted_config()["agents"][0]["capability"]
    r = run(http, "schema", "run-config", "--check", config_file(tmp_path, body), "--json")
    server = http.post("runs", json=body).json()
    assert r.exit_code == 1 and server["code"] == "research_field_locked"
    assert sorted(e["path"] for e in r.json["errors"]) == sorted(e["path"] for e in server["errors"])


@pytest.mark.parametrize("text", ["{not json", None], ids=["bad json", "missing"])
def test_check_of_a_bad_file_is_file_invalid(app, tmp_path, text):
    http, _ = app
    path = config_file(tmp_path, text) if text else str(tmp_path / "none.json")
    r = run(http, "schema", "run-config", "--check", path, "--json")
    assert r.exit_code == 1 and r.json["code"] == "file_invalid"


def test_check_sends_one_get_and_no_write(tmp_path):
    sent = []
    reply = httpx2.MockTransport(
        lambda req: sent.append(req) or httpx2.Response(200, json={"type": "object", "properties": {}, "$defs": {}})
    )
    r = run(
        httpx2.Client(base_url="http://fake/api", transport=reply), "schema", "run-config", "--check", config_file(tmp_path, {"kind": "x"})
    )
    assert r.exit_code == 0 and [(q.method, q.url.path) for q in sent] == [("GET", "/api/schema/run-config")]


def test_check_with_another_part_is_a_usage_error(app, tmp_path):
    http, _ = app
    assert run(http, "schema", "run-config", "--check", config_file(tmp_path, {}), "--policy", "llm").exit_code == 2


def test_check_needs_no_dev_dependency():
    """CR-20 m1: jsonschema is a runtime requirement, so --check works after `uv sync --no-dev` or `uv tool install`."""
    assert any(r.split(";")[0].replace(" ", "").startswith("jsonschema==") for r in requires("collusion-sim"))
