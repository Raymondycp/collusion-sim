"""Experiments commands against the 0.8.0 routes: estimate, start (paid guard, dry run, --wait end rules), show and list."""

import copy
import hashlib
import json
import shutil
import zipfile

import httpx2
import pytest
import yaml
from click.testing import CliRunner
from conftest import ROOT, Result
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.app import create_app
from simcli.main import cli

SPEC = yaml.safe_load((ROOT / "docs" / "sim" / "api" / "openapi.yaml").read_text(encoding="utf-8"))
PILOT = SPEC["components"]["examples"]["ExperimentPilot"]["value"]
# A profile that exists (the estimate and start check the binding first, CR-24), so the paid-call switch is what refuses.
ENV_PROFILE = {"SIM_PROFILE_ENVTEST_BASE_URL": "https://example.invalid/v1", "SIM_PROFILE_ENVTEST_KEY": "not-a-real-key-0000"}
LLM = {"id": "A", "role": "seller", "policy": {"kind": "llm", "llm": {"provider_profile_id": "envtest", "model": "m"}}}


def validate(body, name: str):
    Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]}).validate(body)


def run(http, *args: str, sleep=None, clock=None) -> Result:
    obj = {"http": http, **({"sleep": sleep} if sleep else {}), **({"clock": clock} if clock else {})}
    r = CliRunner().invoke(cli, list(args), obj=obj, catch_exceptions=False)
    return Result(r.exit_code, r.stdout, r.stderr)


def request_file(tmp_path, body, name="exp.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def pilot(**changes) -> dict:
    return {**copy.deepcopy(PILOT), "conditions": ["C1"], "seeds": [1], **changes}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    root = tmp_path_factory.mktemp("exproot")
    for folder in ("scenarios", "conditions"):
        shutil.copytree(ROOT / folder, root / folder)
    with pytest.MonkeyPatch.context() as env:
        for k, v in ENV_PROFILE.items():
            env.setenv(k, v)
        with TestClient(create_app(root, use_dbos=False), base_url="http://testserver/api") as http:
            yield http


def recorder(responses):
    """A fake server: answers GETs from `responses` in order and records every request."""
    sent = []

    def handle(req):
        sent.append(req)
        return httpx2.Response(responses[0][0], **responses.pop(0)[1]) if responses else httpx2.Response(500)

    return httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(handle)), sent


def test_estimate_is_the_api_body_and_matches_the_contract(app, tmp_path):
    body = pilot()
    r = run(app, "experiments", "estimate", "--file", request_file(tmp_path, body), "--json")
    assert r.exit_code == 0, r.stdout
    validate(r.json, "ExperimentEstimate")
    assert r.json == app.post("experiments/estimate", json=body).json() and r.json["n_cells"] == 1
    assert app.get("experiments").json() == []


def test_estimate_human_output_has_the_totals(app, tmp_path):
    r = run(app, "experiments", "estimate", "--file", request_file(tmp_path, pilot()))
    assert r.exit_code == 0 and "n_cells" in r.stdout and "usd_max" in r.stdout


def test_a_scripted_experiment_from_start_to_show(app, tmp_path):
    r = run(app, "experiments", "start", "--file", request_file(tmp_path, pilot(name="cli smoke")), "--wait", "--max-wait", "120", "--json")
    assert r.exit_code == 0, r.stdout
    validate(r.json, "Experiment")
    eid = r.json["id"]
    assert r.json["status"] == "completed" and all(c["status"] == "completed" for c in r.json["cells"])
    shown = run(app, "experiments", "show", eid, "--json")
    assert shown.exit_code == 0 and shown.json == app.get(f"experiments/{eid}").json()
    listed = run(app, "experiments", "list", "--json")
    assert listed.exit_code == 0 and eid in [e["id"] for e in listed.json]
    for item in listed.json:
        validate(item, "ExperimentSummary")
    human = run(app, "experiments", "show", eid)
    cell_line = next(line for line in human.stdout.splitlines() if line.startswith("C1 "))
    assert cell_line.split()[:3] == ["C1", "1", "completed"] and r.json["cells"][0]["parent"]["run_id"] in cell_line


def test_export_writes_the_served_zip_and_refuses_an_existing_file(app, tmp_path):
    started = run(app, "experiments", "start", "--file", request_file(tmp_path, pilot()), "--wait", "--max-wait", "120", "--json")
    eid = started.json["id"]
    out = tmp_path / "exp.zip"
    r = run(app, "experiments", "export", eid, "-o", str(out), "--json")
    assert r.exit_code == 0, r.stdout
    data = out.read_bytes()
    assert data == app.get(f"experiments/{eid}/export", params={"format": "zip"}).content
    assert r.json == {
        "path": str(out.resolve()),
        "bytes": len(data),
        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "format": "zip",
        "experiment_id": eid,
    }
    names = zipfile.ZipFile(out).namelist()
    assert names[0] == "experiment.json" and names[-1] == "manifest.json" and any(n.endswith("/trail.jsonl") for n in names)
    again = run(app, "experiments", "export", eid, "-o", str(out), "--json")
    assert again.exit_code == 1 and again.json["code"] == "path_refused" and out.read_bytes() == data
    missing = run(app, "experiments", "export", "exp_00000000000000000000000000", "-o", str(tmp_path / "n.zip"), "--json")
    assert missing.json["code"] == "not_found" and not (tmp_path / "n.zip").exists()


@pytest.mark.parametrize(
    "body",
    [pilot(agents=[LLM, PILOT["agents"][1]]), pilot(agents_by_condition={"C1": [LLM, PILOT["agents"][1]]})],
    ids=["agents", "agents_by_condition"],
)
def test_an_llm_agent_needs_allow_paid_and_nothing_is_sent(tmp_path, body):
    http, sent = recorder([])
    r = run(http, "experiments", "start", "--file", request_file(tmp_path, body), "--json")
    assert r.exit_code == 1 and r.json["code"] == "paid_flag_required" and sent == []


def test_a_dry_run_prints_the_request_and_sends_nothing(tmp_path):
    http, sent = recorder([])
    body = pilot(agents=[LLM, PILOT["agents"][1]])
    r = run(http, "experiments", "start", "--file", request_file(tmp_path, body), "--dry-run", "--json")
    assert r.exit_code == 0 and r.json == {"dry_run": True, "method": "POST", "path": "/experiments", "params": {}, "body": body}
    assert sent == []


def test_allow_paid_still_meets_the_server_switch(app, tmp_path):
    body = pilot(agents=[LLM, PILOT["agents"][1]])
    r = run(app, "experiments", "start", "--file", request_file(tmp_path, body), "--allow-paid", "--json")
    assert r.exit_code == 1 and r.json["code"] == "paid_calls_disabled"


def test_a_research_field_is_refused_by_the_server(app, tmp_path):
    agents = [{**a, "capability": {"grants": [], "max_queries_per_round": 1}} for a in PILOT["agents"]]
    r = run(app, "experiments", "start", "--file", request_file(tmp_path, pilot(agents=agents)), "--json")
    assert r.exit_code == 1 and r.json["code"] == "research_field_locked"


def test_a_seed_outside_the_split_is_refused(app, tmp_path):
    r = run(app, "experiments", "estimate", "--file", request_file(tmp_path, pilot(seeds=[999999])), "--json")
    assert r.exit_code == 1 and r.json["code"] == "seed_not_in_split"


def experiment(status, cell_status="running", run_status="running", error=None):
    run_state = {"run_id": "run_" + "0" * 26, "status": run_status, "end_reason": None}
    cell = {"condition_id": "C1", "seed": 1, "status": cell_status, "error": error, "parent": run_state, "branches": []}
    return {"id": "exp_" + "1" * 26, "status": status, "cells": [cell], "spend": {"calls": 0, "usd": 0}}


def test_wait_stops_on_a_paused_run(tmp_path):
    http, _ = recorder([(202, {"json": experiment("running")}), (200, {"json": experiment("running", "paused", "paused")})])
    r = run(http, "experiments", "start", "--file", request_file(tmp_path, pilot()), "--wait", "--json", sleep=lambda s: None)
    assert r.exit_code == 1 and r.json["code"] == "run_not_live"


def test_wait_gives_up_after_max_wait(tmp_path):
    now = [0.0]
    http, _ = recorder([(202, {"json": experiment("running")})] + [(200, {"json": experiment("running")})] * 5)
    sleep = lambda s: now.__setitem__(0, now[0] + s)  # noqa: E731
    args = ("experiments", "start", "--file", request_file(tmp_path, pilot()), "--wait", "--max-wait", "2", "--json")
    r = run(http, *args, sleep=sleep, clock=lambda: now[0])
    assert r.exit_code == 1 and r.json["code"] == "wait_timeout"


def test_wait_ends_on_ended(tmp_path):
    http, _ = recorder([(202, {"json": experiment("running")}), (200, {"json": experiment("ended", "failed", "failed")})])
    r = run(http, "experiments", "start", "--file", request_file(tmp_path, pilot()), "--wait", "--json", sleep=lambda s: None)
    assert r.exit_code == 0 and r.json["status"] == "ended"


def test_show_encodes_the_id_and_an_unknown_id_is_not_found(app):
    http, sent = recorder([(200, {"json": experiment("completed")})])
    run(http, "experiments", "show", "a/b", "--json")
    assert sent[0].method == "GET" and sent[0].url.raw_path == b"/api/experiments/a%2Fb"
    r = run(app, "experiments", "show", "exp_" + "Z" * 26, "--json")
    assert r.exit_code == 1 and r.json["code"] == "not_found"


BLOCK = {"code": "paid_calls_disabled", "message": "Paid calls are off.", "at": "2026-10-22T10:00:00Z"}


def test_wait_stops_on_a_blocked_experiment_and_names_the_cause(tmp_path):
    done = experiment("blocked", "blocked", "completed", error=BLOCK)
    http, _ = recorder([(202, {"json": experiment("running")}), (200, {"json": done})])
    r = run(http, "experiments", "start", "--file", request_file(tmp_path, pilot()), "--wait", "--json", sleep=lambda s: None)
    assert r.exit_code == 1 and r.json["code"] == "run_not_live"
    assert r.json["errors"] == [{"path": "cells/0", "message": "C1 seed 1: paid_calls_disabled: Paid calls are off."}]


def test_show_has_the_cell_error_code(tmp_path):
    http, _ = recorder([(200, {"json": experiment("blocked", "blocked", "completed", error=BLOCK)})])
    r = run(http, "experiments", "show", "exp_" + "1" * 26)
    cell_line = next(line for line in r.stdout.splitlines() if line.startswith("C1 "))
    assert r.exit_code == 0 and cell_line.split()[2] == "blocked" and cell_line.endswith("paid_calls_disabled")
    assert "C1 seed 1: paid_calls_disabled: Paid calls are off." in r.stdout and "restart the server" in r.stdout


def test_wait_on_blocked_says_to_restart_the_server(tmp_path):
    done = experiment("blocked", "blocked", "completed", error=BLOCK)
    http, _ = recorder([(202, {"json": experiment("running")}), (200, {"json": done})])
    r = run(http, "experiments", "start", "--file", request_file(tmp_path, pilot()), "--wait", "--json", sleep=lambda s: None)
    assert "restart the server (`serve`)" in r.json["message"]


def test_show_and_start_print_the_stored_estimate(tmp_path):
    body = {**experiment("completed", "completed", "completed"), "estimate": {"calls_max": 40, "usd_max": None, "within_budget": None}}
    for args, status in (
        (("experiments", "show", body["id"]), 200),
        (("experiments", "start", "--file", request_file(tmp_path, pilot())), 202),
    ):
        http, _ = recorder([(status, {"json": body})])
        lines = {x.split()[0]: x.split()[1:] for x in run(http, *args).stdout.splitlines() if x.startswith("estimate.")}
        assert lines == {"estimate.calls_max": ["40"], "estimate.usd_max": [], "estimate.within_budget": []}
