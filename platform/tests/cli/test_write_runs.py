"""Run start, control, export and branch create; the paid guard (CLI-DESIGN §3.5, §3.6, §6)."""

import json
import threading
import time
import zipfile

import httpx2
import pytest
from conftest import scripted_config

LLM_AGENT = {"id": "A", "role": "seller", "policy": {"kind": "llm", "llm": {"provider_profile_id": "prof_none", "model": "m"}}}


def write(tmp_path, name: str, body) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


@pytest.fixture
def done_run(server, tmp_path):
    out = server("runs", "start", "--config", write(tmp_path, "c.json", scripted_config()), "--wait", "--json").json
    return out["run_ids"][0]


def test_start_wait_prints_the_batch_and_final_summaries(server, tmp_path):
    out = server("runs", "start", "--config", write(tmp_path, "c.json", scripted_config(seeds=(3, 4))), "--wait", "--json").json
    assert len(out["run_ids"]) == 2 and out["batch_id"]
    assert sorted(r["id"] for r in out["runs"]) == sorted(out["run_ids"]) and {r["status"] for r in out["runs"]} == {"completed"}
    human = server("runs", "start", "--config", write(tmp_path, "c.json", scripted_config(seeds=(5,))), "--wait").stdout
    assert human.startswith("batch_id") and "completed" in human


def test_an_llm_config_needs_allow_paid_and_the_server_switch_still_decides(server, tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    config = write(tmp_path, "llm.json", {**scripted_config(), "agents": [LLM_AGENT, scripted_config()["agents"][1]]})
    before = len(server.http.get("runs").json()["items"])
    refused = server("runs", "start", "--config", config, "--json")
    assert refused.exit_code == 1 and refused.json["code"] == "paid_flag_required" and "A" in refused.json["message"]
    server_side = server("runs", "start", "--config", config, "--allow-paid", "--json")
    assert server_side.exit_code == 1 and server_side.json["code"] == "paid_calls_disabled"
    assert len(server.http.get("runs").json()["items"]) == before
    preview = server("runs", "start", "--config", config, "--dry-run", "--json").json
    assert preview["dry_run"] and preview["path"] == "/runs" and len(server.http.get("runs").json()["items"]) == before


def test_pause_resume_and_stop_a_live_run(server, tmp_path):
    from simcore.scripted import GrimTrigger

    gate, act = threading.Event(), GrimTrigger.act

    def slow(self, obs, tools):
        if obs.t == 2:
            gate.wait(10)
        return act(self, obs, tools)

    GrimTrigger.act = slow
    try:
        (rid,) = server("runs", "start", "--config", write(tmp_path, "c.json", scripted_config()), "--json").json["run_ids"]
        assert server("runs", "pause", rid, "--json").exit_code == 0
        gate.set()
        for _ in range(100):
            if server.http.get(f"runs/{rid}").json()["status"] == "paused":
                break
            time.sleep(0.05)
        follow = server("runs", "events", rid, "--follow", "--types", "run_ended", "--json", sleep=lambda s: None)
        assert follow.exit_code == 1 and json.loads(follow.stdout.splitlines()[-1])["code"] == "run_not_live"
        assert server("runs", "resume", rid, "--json").exit_code == 0
        server.http.app.state.svc.wait(rid, timeout=30)
    finally:
        GrimTrigger.act = act
    assert server.http.get(f"runs/{rid}").json()["status"] == "completed"
    ended = server("runs", "stop", rid, "--json")
    assert ended.exit_code == 1 and ended.json["code"] == "run_not_active"


def test_export_formats(server, done_run, tmp_path):
    jsonl = server("runs", "export", done_run, "--format", "jsonl", "-o", str(tmp_path / "ep.jsonl"), "--json").json
    assert jsonl["bytes"] == (tmp_path / "ep.jsonl").stat().st_size and jsonl["sha256"].startswith("sha256:")
    assert server("episode", "validate", jsonl["path"], "--json").json["run_id"] == done_run
    server("runs", "export", done_run, "-o", str(tmp_path / "run.zip"), "--json")
    assert zipfile.ZipFile(tmp_path / "run.zip").namelist() == ["episode.jsonl", "meta.json", "trail.jsonl", "manifest.json"]
    csv = server("runs", "export", done_run, "--format", "csv_rounds", "-o", str(tmp_path / "r.csv"), "--json").json
    assert csv["format"] == "csv_rounds" and len((tmp_path / "r.csv").read_text(encoding="utf-8").splitlines()) == 61
    assert not list(tmp_path.glob("*.part"))


def test_export_refuses_server_folders_existing_files_and_bad_flags(server, done_run, tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_ROOT", str(tmp_path))
    for folder in ("data", "cache", "runs", "scenarios"):
        inside = server("runs", "export", done_run, "-o", str(tmp_path / folder / "x.zip"), "--json")
        assert inside.exit_code == 1 and inside.json["code"] == "path_refused" and not (tmp_path / folder).exists()
    target = tmp_path / "out.zip"
    target.write_bytes(b"keep")
    exists = server("runs", "export", done_run, "-o", str(target), "--json")
    assert exists.json["code"] == "path_refused" and target.read_bytes() == b"keep"
    assert server("runs", "export", done_run, "-o", str(target), "--overwrite", "--json").exit_code == 0 and target.read_bytes() != b"keep"
    assert server("runs", "export", done_run, "--format", "jsonl", "--include-branches", "-o", str(tmp_path / "y"), "--json").exit_code == 2
    missing = server("runs", "export", "run_nope", "-o", str(tmp_path / "z.zip"), "--json")
    assert missing.json["code"] == "not_found" and not (tmp_path / "z.zip").exists()


def test_branches_create_and_dry_run(server, done_run, tmp_path):
    probe = {"at": 15, "rounds": 5, "n": 2, "intervention": {"type": "probe", "target": "A", "price": "nash", "rounds": 1}}
    request = write(tmp_path, "b.json", probe)
    dry = server("branches", "create", done_run, "--file", request, "--dry-run", "--json").json
    assert dry["body"] == probe and server.http.get(f"runs/{done_run}/branches").json() == []
    out = server("branches", "create", done_run, "--file", request, "--json").json
    assert len(out["run_ids"]) == 2
    late = server("branches", "create", done_run, "--file", write(tmp_path, "late.json", {**probe, "at": 99}), "--json")
    assert late.exit_code == 1 and late.json["code"] == "snapshot_missing"


def test_branching_an_llm_run_needs_allow_paid(run_cli, tmp_path):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx2.Response(409, json={"title": "x", "status": 409, "code": "paid_calls_disabled", "message": "off"})
        return httpx2.Response(200, json={"id": "run_x", "status": "completed", "models": ["gpt-x"]})

    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(handler))
    request = write(tmp_path, "b.json", {"at": 2, "rounds": 1, "n": 1, "intervention": {"type": "placebo"}})
    r = run_cli(http, "branches", "create", "run_x", "--file", request, "--json")
    assert r.exit_code == 1 and r.json["code"] == "paid_flag_required" and "gpt-x" in r.json["message"] and posts == []
    r = run_cli(http, "branches", "create", "run_x", "--file", request, "--allow-paid", "--json")
    assert r.json["code"] == "paid_calls_disabled" and posts == ["/api/runs/run_x/branches"]
