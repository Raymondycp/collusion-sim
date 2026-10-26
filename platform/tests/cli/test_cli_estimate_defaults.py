"""Unit 8 (contract 0.9.0): `runs estimate --config F`, `defaults`, and `.env` profiles are read only."""

import json
import shutil

import httpx2
import pytest
import yaml
from click.testing import CliRunner
from conftest import ROOT, Result, scripted_config
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.app import create_app
from simcli.main import cli

SPEC = yaml.safe_load((ROOT / "docs" / "sim" / "api" / "openapi.yaml").read_text(encoding="utf-8"))
EXAMPLE_ESTIMATE = SPEC["paths"]["/runs/estimate"]["post"]["responses"]["200"]["content"]["application/json"]["example"]
EXAMPLE_DEFAULTS = SPEC["paths"]["/defaults"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
DEFAULTS_ENV = {"SIM_DEFAULT_SELLER_MODEL": "m-seller", "SIM_DEFAULT_SELLER_B_PROFILE": "prof_x", "SIM_DEFAULT_JUDGE_MODEL": "m-judge"}
ENV_PROFILE = {"SIM_PROFILE_ENVTEST_BASE_URL": "https://example.invalid/v1", "SIM_PROFILE_ENVTEST_KEY": "not-a-real-key-0000"}


def validate(body, name: str):
    Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]}).validate(body)


def run(http, *args: str, input: str | None = None) -> Result:
    r = CliRunner().invoke(cli, list(args), obj={"http": http}, input=input, catch_exceptions=False)
    return Result(r.exit_code, r.stdout, r.stderr)


def fake(status: int, body):
    sent = []
    reply = httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(status, json=body))
    return httpx2.Client(base_url="http://fake/api", transport=reply), sent


def config_file(tmp_path, body, name="config.json") -> str:
    path = tmp_path / name
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return str(path)


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    root = tmp_path_factory.mktemp("u8root")
    shutil.copytree(ROOT / "scenarios", root / "scenarios")
    with pytest.MonkeyPatch.context() as env:
        for k, v in {**DEFAULTS_ENV, **ENV_PROFILE}.items():
            env.setenv(k, v)
        with TestClient(create_app(root, use_dbos=False), base_url="http://testserver/api") as http:
            yield http


# runs estimate --config F


def test_estimate_is_the_api_body_and_stores_nothing(app, tmp_path):
    body = scripted_config(seeds=(1, 2))
    r = run(app, "runs", "estimate", "--config", config_file(tmp_path, body), "--json")
    assert r.exit_code == 0 and r.json == app.post("runs/estimate", json=body).json()
    validate(r.json, "CostEstimate")
    assert r.json["n_runs"] == 2 and app.get("runs").json()["items"] == []


def test_estimate_of_an_llm_config_needs_no_allow_paid(tmp_path):
    http, sent = fake(200, EXAMPLE_ESTIMATE)
    config = scripted_config()
    config["agents"][0]["policy"] = {"kind": "llm", "llm": {"provider_profile_id": "opencode_chat", "model": "m"}}
    r = run(http, "runs", "estimate", "--config", config_file(tmp_path, config), "--json")
    assert r.exit_code == 0 and [(q.method, q.url.path) for q in sent] == [("POST", "/api/runs/estimate")]
    assert json.loads(sent[0].content) == config


def test_estimate_has_no_write_options():
    params = {p.name for p in cli.commands["runs"].commands["estimate"].params}
    assert not params & {"allow_paid", "dry_run", "wait"}


def test_estimate_human_output_has_totals_rate_window_and_warnings(tmp_path):
    http, _ = fake(200, EXAMPLE_ESTIMATE)
    out = run(http, "runs", "estimate", "--config", config_file(tmp_path, scripted_config())).stdout
    lines = {x.split()[0]: x.split()[1:] for x in out.splitlines() if x.split()}
    assert lines["calls_max"] == ["1800"] and lines["within_budget"] == ["True"] and lines["n_runs"] == ["2"]
    window = next(x.split() for x in out.splitlines() if x.startswith("opencode_chat"))
    assert window[:4] == ["opencode_chat", "500", "0", "500"] and window[-1] == "False"
    assert EXAMPLE_ESTIMATE["warnings"][0][:60] in out and EXAMPLE_ESTIMATE["assumptions"][0][:60] in out


def test_estimate_passes_a_validation_problem_through(app, tmp_path):
    config = scripted_config()
    config["agents"][0]["policy"] = {"kind": "llm"}
    r = run(app, "runs", "estimate", "--config", config_file(tmp_path, config), "--json")
    assert r.exit_code == 1 and r.json["code"] == "validation_error"
    assert [e["path"] for e in r.json["errors"]] == ["agents/0/policy/llm"]


@pytest.mark.parametrize("text", ["{not json", None], ids=["bad json", "missing"])
def test_estimate_of_a_bad_file_is_file_invalid_and_sends_nothing(tmp_path, text):
    http, sent = fake(200, {})
    path = config_file(tmp_path, text) if text else str(tmp_path / "none.json")
    r = run(http, "runs", "estimate", "--config", path, "--json")
    assert r.exit_code == 1 and r.json["code"] == "file_invalid" and sent == []


# defaults


def test_defaults_is_the_api_body(app):
    r = run(app, "defaults", "--json")
    assert r.exit_code == 0 and r.json == app.get("defaults").json()
    validate(r.json, "Defaults")
    assert r.json["seller"]["model"] == "m-seller" and r.json["seller"]["agents"]["B"]["profile_id"] == "prof_x"


def test_defaults_human_output_has_one_line_per_role_and_agent():
    http, sent = fake(200, EXAMPLE_DEFAULTS)
    out = run(http, "defaults").stdout
    rows = [x.split() for x in out.splitlines()[1:] if x.split()]
    assert ["seller", "opencode_chat", "kimi-k2"] in [r for r in rows if len(r) == 3]
    assert ["seller", "B", "glm-4.6"] in [r for r in rows if len(r) == 3]
    assert {r[0] for r in rows} == set(EXAMPLE_DEFAULTS)
    assert [(q.method, q.url.path) for q in sent] == [("GET", "/api/defaults")]


# .env profiles are read only


@pytest.mark.parametrize(
    "args, stdin",
    [
        (["profiles", "update", "envtest", "--name", "x"], None),
        (["profiles", "set-key", "envtest"], "other-key-1111\n"),
        (["profiles", "delete", "envtest"], None),
    ],
    ids=["update", "set-key", "delete"],
)
def test_an_env_profile_is_read_only(app, args, stdin):
    shown = run(app, "profiles", "show", "envtest", "--json").json
    assert shown["source"] == "env" and shown["read_only"] is True
    r = run(app, *args, "--json", input=stdin)
    assert r.exit_code == 1 and r.json["code"] == "profile_read_only"
    assert "not-a-real-key" not in r.stdout + r.stderr and "other-key" not in r.stdout + r.stderr
