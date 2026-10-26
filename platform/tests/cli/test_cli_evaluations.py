"""Evaluations (contract 0.15.0): estimate, start and show a judge job. Scripted backend only; no model call."""

import json
import shutil

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
ITEMS = ("agreement", "offer", "acceptance", "talk_score")
RUBRIC = {
    "id": "items-cli",
    "items": {
        "agreement": {"text": "One seller makes a specific price offer and the other clearly accepts it."},
        "offer": {"text": "One seller proposes a price, a range or a rule."},
        "acceptance": {"text": "The other seller clearly accepts that proposal."},
        "talk_score": {"text": "How much the talk is about coordinating prices.", "anchors": [f"{i}: anchor {i}." for i in range(6)]},
    },
    "guard": "Sharing public information about demand or news is not an offer.",
    "quote_min_words": 3,
    "llm": {
        "paraphrases": [
            {
                "system": f"You audit a pricing study ({i}).",
                "user": "Items:\n{items}\nTranscript:\n{transcript}\n",
                "item_order": list(ITEMS),
            }
            for i in range(3)
        ]
    },
    "jev": {"model": "jev-1.13", "paraphrases": [{k: f"Wording {i} for {k}." for k in ITEMS} for i in range(3)]},
}
TALK = {"price": 10.5, "offer_text": "Seller B, shall we both hold 10.5?", "channel_id": "private_A_B"}
ACCEPT = {"price": 10.5, "offer_text": "agent A: ok, 10.5 it is.", "channel_id": "private_A_B"}
LLM_BACKEND = {"provider_profile_id": "p", "model": "m", "model_family": "other"}


def validate(body, name: str):
    Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]}).validate(body)


def run(http, *args: str) -> Result:
    r = CliRunner().invoke(cli, list(args), obj={"http": http, "sleep": lambda s: None}, catch_exceptions=False)
    return Result(r.exit_code, r.stdout, r.stderr)


def request_file(tmp_path, body, name="eval.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def job(run_ids, **changes) -> dict:
    return {"run_ids": run_ids, "block": "wording", "view": "talk_only", "item_set_id": "items-cli", "backends": {"scripted": {}}} | changes


@pytest.fixture(scope="module")
def judged(tmp_path_factory):
    """Two finished C1 pilot runs (seeds 1, 2) with scripted talkers, and the judge rubric in place."""
    root = tmp_path_factory.mktemp("evalroot")
    for folder in ("scenarios", "conditions"):
        shutil.copytree(ROOT / folder, root / folder)
    (root / "conditions" / "judge-rubric.yaml").write_text(yaml.safe_dump(RUBRIC, sort_keys=False), encoding="utf-8")
    with TestClient(create_app(root, use_dbos=False), base_url="http://testserver/api") as http:
        agents = [
            {"id": "A", "role": "seller", "policy": {"kind": "scripted_talker", "params": TALK}},
            {"id": "B", "role": "seller", "policy": {"kind": "scripted_talker", "params": ACCEPT}},
        ]
        config = {"kind": "research", "condition_id": "C1", "split": "pilot", "seeds": [1, 2], "agents": agents}
        r = http.post("runs", json=config | {"clock": {"mode": "sync"}, "budget": {"max_usd": 1.0}})
        assert r.status_code == 202, r.text
        run_ids = r.json()["run_ids"]
        for rid in run_ids:
            http.app.state.svc.wait(rid, timeout=60)
        yield http, run_ids


def test_estimate_is_the_api_body_and_stores_nothing(judged, tmp_path):
    http, run_ids = judged
    r = run(http, "evaluations", "estimate", "--file", request_file(tmp_path, job(run_ids)), "--json")
    assert r.exit_code == 0 and r.json == http.post("evaluations/estimate", json=job(run_ids)).json()
    validate(r.json, "EvaluationEstimate")
    assert (r.json["episodes"], r.json["excluded"], r.json["per_backend"]["scripted"]["calls"]) == (2, [], 6)
    assert http.get("evaluations").json() == []


def test_estimate_human_output_lists_backends_and_excluded_runs(judged, tmp_path):
    http, run_ids = judged
    out = run(http, "evaluations", "estimate", "--file", request_file(tmp_path, job([*run_ids, "run_nope"]))).stdout
    lines = [x.split() for x in out.splitlines() if x.split()]
    assert ["episodes", "2"] in lines and any(x[0] == "scripted" and x[1] == "6" for x in lines)
    assert any(x[:3] == ["2", "run_nope", "not_found"] for x in lines)


def test_a_scripted_job_from_start_to_show(judged, tmp_path):
    http, run_ids = judged
    started = run(http, "evaluations", "start", "--file", request_file(tmp_path, job(run_ids)), "--wait", "--max-wait", "60", "--json")
    assert started.exit_code == 0, started.stderr
    validate(started.json, "Evaluation")
    assert started.json["status"] == "completed" and started.json["per_backend"]["scripted"]["counts"] == {"ok": 6}
    shown = run(http, "evaluations", "show", started.json["id"], "--json")
    assert shown.json == http.get(f"evaluations/{started.json['id']}").json()
    assert {e["run_id"] for e in shown.json["episodes"]} == set(run_ids)
    human = run(http, "evaluations", "show", started.json["id"]).stdout
    assert "completed" in human and all(rid in human for rid in run_ids) and "scripted" in human


@pytest.mark.parametrize("backends", [{"llm": LLM_BACKEND}, {"scripted": {}, "jev": {"provider_profile_id": "p"}}], ids=["llm", "jev"])
def test_a_paid_backend_needs_allow_paid_and_nothing_is_sent(tmp_path, backends):
    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: pytest.fail("sent a request")))
    r = run(http, "evaluations", "start", "--file", request_file(tmp_path, job(["run_x"], backends=backends)), "--json")
    assert r.exit_code == 1 and r.json["code"] == "paid_flag_required"


def test_allow_paid_still_meets_the_server(judged, tmp_path):
    """--allow-paid only passes the CLI guard: a server with paid calls off still refuses the llm backend."""
    http, run_ids = judged
    body = job(run_ids, backends={"llm": LLM_BACKEND})
    r = run(http, "evaluations", "start", "--file", request_file(tmp_path, body), "--allow-paid", "--json")
    assert r.exit_code == 1 and r.json["code"] == "paid_calls_disabled"
    assert all("llm" not in e["backends"] for e in http.get("evaluations").json())


def test_a_dry_run_prints_the_request_and_sends_nothing(tmp_path):
    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: pytest.fail("sent a request")))
    body = job(["run_x"], backends={"llm": LLM_BACKEND})
    r = run(http, "evaluations", "start", "--file", request_file(tmp_path, body), "--dry-run", "--json")
    assert r.exit_code == 0 and r.json["dry_run"] is True and r.json["body"] == body


def test_wait_gives_up_after_max_wait(tmp_path):
    queued = {"id": "eval_1", "status": "running"}
    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: httpx2.Response(200, json=queued)))
    clock = iter([0.0, 0.0, 5.0, 10.0, 15.0])
    r = CliRunner().invoke(
        cli,
        ["evaluations", "start", "--file", request_file(tmp_path, job(["run_x"])), "--wait", "--max-wait", "4", "--json"],
        obj={"http": http, "sleep": lambda s: None, "clock": lambda: next(clock)},
        catch_exceptions=False,
    )
    body = json.loads(r.stdout)
    assert r.exit_code == 1 and (body["code"], body["evaluation_id"]) == ("wait_timeout", "eval_1") and "eval_1" in body["message"]
    assert r.stderr.startswith("evaluation eval_1 running; waiting"), "CR-39 m1: the id is printed before the wait"


def test_show_encodes_the_id_and_an_unknown_id_is_not_found(judged):
    http, _ = judged
    r = run(http, "evaluations", "show", "eval_nope", "--json")
    assert r.exit_code == 1 and r.json["code"] == "not_found"
    sent = []
    fake = httpx2.Client(
        base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(404, json=r.json))
    )
    run(fake, "evaluations", "show", "a/b", "--json")
    assert sent[0].url.raw_path == b"/api/evaluations/a%2Fb"


def fake_job(backends: dict, sent: list) -> httpx2.Client:
    job = {"id": "eval_1", "status": "failed", "backends": backends}
    return httpx2.Client(
        base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(200, json=job))
    )


def test_resume_of_a_paid_job_needs_allow_paid_and_sends_only_the_read(tmp_path):
    sent = []
    r = run(fake_job({"llm": LLM_BACKEND}, sent), "evaluations", "resume", "eval_1", "--json")
    assert r.exit_code == 1 and r.json["code"] == "paid_flag_required"
    assert [(q.method, q.url.path) for q in sent] == [("GET", "/api/evaluations/eval_1")]
    sent.clear()
    r = run(fake_job({"llm": LLM_BACKEND}, sent), "evaluations", "resume", "eval_1", "--allow-paid", "--dry-run", "--json")
    assert r.exit_code == 0 and (r.json["method"], r.json["path"]) == ("POST", "/evaluations/eval_1/resume")
    assert [q.method for q in sent] == ["GET"]


def test_resume_reaches_the_route_and_the_server_refuses_a_job_that_did_not_fail(judged, tmp_path):
    http, run_ids = judged
    done = run(http, "evaluations", "start", "--file", request_file(tmp_path, job(run_ids)), "--wait", "--max-wait", "60", "--json").json
    r = run(http, "evaluations", "resume", done["id"], "--json")
    assert (r.exit_code, r.json["code"], r.json["status"]) == (1, "conflict", 409)
    assert run(http, "evaluations", "resume", "eval_nope", "--json").json["code"] == "not_found"


def test_wait_on_a_job_that_ends_failed_is_exit_1_with_its_error(tmp_path):
    error = {"code": "auth", "message": "The endpoint rejected the key."}
    replies = {
        "POST": {"id": "eval_1", "status": "queued"},
        "GET": {"id": "eval_1", "status": "failed", "error": error, "backends": {"scripted": {}}},
    }
    http = httpx2.Client(
        base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: httpx2.Response(200, json=replies[req.method]))
    )
    r = run(http, "evaluations", "start", "--file", request_file(tmp_path, job(["run_x"])), "--wait", "--max-wait", "60", "--json")
    assert r.exit_code == 1 and (r.json["code"], r.json["evaluation_id"], r.json["evaluation_error"]) == (
        "evaluation_failed",
        "eval_1",
        error,
    )
    assert run(http, "evaluations", "resume", "eval_1", "--wait", "--json").json["code"] == "evaluation_failed"


def test_a_json_file_with_a_byte_order_mark_is_read(tmp_path):
    """CR-44 4: Windows PowerShell 5.1 `Set-Content -Encoding utf8` starts the file with a BOM."""
    sent = []
    http = httpx2.Client(
        base_url="http://fake/api",
        transport=httpx2.MockTransport(lambda req: sent.append(json.loads(req.content)) or httpx2.Response(200, json={})),
    )
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(job(["run_x"])), encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert run(http, "evaluations", "estimate", "--file", str(path), "--json").exit_code == 0 and sent == [job(["run_x"])]
