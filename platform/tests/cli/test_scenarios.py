"""Scenario drafts, generators, check, freeze, reference, and the market preview (CLI-DESIGN §3.3)."""

import json

import httpx2


def write(tmp_path, name: str, body) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def template_draft(server) -> dict:
    body = {"generator_id": "template", "inputs": {"template": "cost_shock_public"}}
    return server.http.post("scenarios/generate", json=body).json()["draft"]


def test_draft_check_freeze_flow(server, tmp_path):
    draft = write(tmp_path, "draft.json", template_draft(server))
    created = server("scenarios", "create", "--file", draft, "--json").json
    sid = created["id"]
    assert created["status"] == "draft" and sid in server("scenarios", "list", "--status", "draft").stdout
    report = server("scenarios", "check", sid, "--json").json
    assert report["ok"] and server("scenarios", "check", sid).stdout.startswith("ok: True")
    assert server("scenarios", "freeze", sid, "--json").exit_code == 2
    frozen = server("scenarios", "freeze", sid, "--approved-by", "Raymond", "--json").json
    assert frozen["status"] == "frozen" and frozen["approved_by"] == "Raymond" and frozen["hash"].startswith("sha256:")
    refused = server("scenarios", "replace", sid, "--file", draft, "--json")
    assert refused.exit_code == 1 and refused.json["code"] == "scenario_frozen"
    assert server("scenarios", "show", sid, "--json").json["hash"] == frozen["hash"]


def test_replace_and_delete_a_draft(server, tmp_path):
    draft = template_draft(server)
    sid = server("scenarios", "create", "--file", write(tmp_path, "d.json", draft), "--json").json["id"]
    edited = server("scenarios", "replace", sid, "--file", write(tmp_path, "e.json", {**draft, "name": "edited"}), "--json").json
    assert edited["version"] == 2 and edited["name"] == "edited"
    assert server("scenarios", "delete", sid, "--json").json == {"deleted": sid}
    assert server("scenarios", "show", sid, "--json").json["code"] == "not_found"


def test_generate_returns_a_draft_and_save_stores_it(server, tmp_path):
    inputs = write(tmp_path, "inputs.json", {"n_events": 2})
    before = server.http.get("scenarios").json()
    out = server("scenarios", "generate", "random", "--inputs", inputs, "--seed", "4", "--json").json
    assert out["check"]["ok"] and out["draft"]["provenance"]["seed"] == 4 and server.http.get("scenarios").json() == before
    saved = server("scenarios", "generate", "random", "--inputs", inputs, "--seed", "4", "--save", "--json").json
    assert saved["draft"] == out and saved["saved"]["status"] == "draft"
    assert len(server.http.get("scenarios").json()) == len(before) + 1
    unknown = server("scenarios", "generate", "nope", "--inputs", inputs, "--json")
    assert unknown.exit_code == 1 and unknown.json["code"] == "unknown_generator"


def test_a_paid_generator_needs_allow_paid_and_sends_nothing_without_it(run_cli, tmp_path):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx2.Response(409, json={"title": "x", "status": 409, "code": "paid_calls_disabled", "message": "off"})
        return httpx2.Response(200, json=[{"id": "llm", "version": "1", "name": "LLM", "paid": True, "deterministic": False}])

    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(handler))
    inputs = write(tmp_path, "inputs.json", {"brief": "x"})
    r = run_cli(http, "scenarios", "generate", "llm", "--inputs", inputs, "--json")
    assert r.exit_code == 1 and r.json["code"] == "paid_flag_required" and posts == []
    r = run_cli(http, "scenarios", "generate", "llm", "--inputs", inputs, "--allow-paid", "--json")
    assert r.json["code"] == "paid_calls_disabled" and posts == ["/api/scenarios/generate"]


def test_dry_run_prints_the_request_and_saves_nothing(server, tmp_path):
    draft = template_draft(server)
    before = server.http.get("scenarios").json()
    r = server("scenarios", "create", "--file", write(tmp_path, "d.json", draft), "--dry-run", "--json")
    assert r.exit_code == 0 and r.json == {"dry_run": True, "method": "POST", "path": "/scenarios", "params": {}, "body": draft}
    assert server.http.get("scenarios").json() == before
    assert "dry_run" in server("scenarios", "freeze", "scn_mock_demo", "--approved-by", "Raymond", "--dry-run").stdout


def test_reference_and_market_preview(server, tmp_path):
    ref = server("scenarios", "reference", "scn_mock_demo", "--until", "5", "--json").json
    assert [r["t"] for r in ref["rounds"]] == [1, 2, 3, 4, 5]
    header = server("scenarios", "reference", "scn_mock_demo", "--until", "3").stdout.splitlines()[0].split()
    assert header[:3] == ["t", "nash", "joint_optimum"]
    linear = {"id": "linear", "params": {"a": 10, "b": 1.5, "d": 1.0, "c": 1.0}}
    market = {"market": {"demand": linear}, "n_sellers": 2, "rounds": 3, "events": []}
    out = server("market", "preview", "--file", write(tmp_path, "m.json", market), "--json").json
    assert [r["nash"] for r in out["rounds"]] == [5.75, 5.75, 5.75]
    assert len(server("market", "preview", "--file", str(tmp_path / "m.json")).stdout.splitlines()) == 4
