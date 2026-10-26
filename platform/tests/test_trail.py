# ruff: noqa: F401, F811 (the fixture fake_client comes from test_d13)
"""DECISIONS-14 §2.2 to §2.4: the sidecar trail.jsonl of a run and the export manifest, on a fake endpoint (no paid call)."""

import hashlib
import json
import sqlite3
import zipfile
from io import BytesIO

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)

from fakellm import tools_offered
from simschema.episode import read_episode
from test_api import KEYLIKE, check
from test_d13 import OFFER, fake_client, run_condition, talker
from test_experiments import body, created, finished
from test_profiles import KEY


def export(client, rid: str, **params) -> zipfile.ZipFile:
    resp = client.get(f"/api/runs/{rid}/export", params={"format": "zip", **params})
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(BytesIO(resp.content))


def trail(z: zipfile.ZipFile, prefix: str = "") -> list[dict]:
    return [json.loads(line) for line in z.read(prefix + "trail.jsonl").decode("utf-8").splitlines()]


def sent_text(body_: dict) -> list[str]:
    return [m["content"] for m in body_["messages"]]


def test_the_trail_has_one_line_per_llm_call_with_the_request_the_tools_and_the_reply(fake_client):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    lines = trail(export(fake_client, rid))
    calls = [e for e in events if e["type"] == "llm_call"]
    assert [ln["seq"] for ln in lines] == [e["seq"] for e in calls], "one line per llm_call, in seq order"
    assert {ln["trail"] for ln in lines} == {"trail.v1"} and all(ln["missing"] is None for ln in lines)
    bodies = {json.dumps(sent_text(b)): b for b in fake_client.fake.bodies}
    for ln, e in zip(lines, calls):
        assert {k: ln[k] for k in ("t", "agent_id", "purpose", "attempt", "model", "request_hash", "input_tokens", "output_tokens")} == {
            k: e[k] for k in ("t", "agent_id", "purpose", "attempt", "model", "request_hash", "input_tokens", "output_tokens")
        }
        *request, response = ModelMessagesTypeAdapter.validate_python(ln["messages"])
        assert all(isinstance(m, ModelRequest) for m in request) and isinstance(response, ModelResponse)
        sent = [p.content for m in request for p in m.parts if isinstance(p, (SystemPromptPart, UserPromptPart))]
        assert json.dumps(sent) in bodies, "the request messages are the ones the endpoint got"
        assert [t["name"] for t in ln["tools"]] == tools_offered(bodies[json.dumps(sent)])
        assert (response.usage.input_tokens, response.usage.output_tokens) == (100, 20)
        assert (ln["allow_text"], ln["settings"]["temperature"]) == (ln["purpose"] == "talk", 0.2)
        assert (ln["charged"], ln["cache_hit"], ln["paid_by_run"]) == (False, False, rid), "a paid call of this run"
        assert response.finish_reason == ln["finish_reason"] == ("tool_call" if ln["purpose"] == "act" else "stop")
        if ln["purpose"] == "act":
            (tool,) = [p for p in response.parts if isinstance(p, ToolCallPart)]
            assert tool.tool_name == "set_price" and tool.args_as_dict() == {"price": 10.5, "plan": "Keep the price."}
            assert [t["name"] for t in ln["tools"]] == ["set_price", "hold"]
        else:
            assert [p.content for p in response.parts if isinstance(p, TextPart)] == [OFFER] and ln["tools"] == []
    assert {ln["purpose"] for ln in lines} == {"talk", "act"}


def fail_first(brain):
    failed = []

    def first_fails(b):
        if not failed:
            failed.append(1)
            return 500
        return brain(b)

    return first_fails


def test_a_failed_call_keeps_its_request_and_a_retry_has_its_own_line(fake_client, tmp_path):
    fake_client.fake.brain = fail_first(talker)
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    first, retry = trail(export(fake_client, rid))[:2]
    assert (first["attempt"], first["error"], first["http_status"], first["missing"]) == (1, "provider_error", 500, "reply")
    assert (retry["attempt"], retry["error"], retry["missing"], retry["agent_id"]) == (2, None, None, first["agent_id"])
    sent = ModelMessagesTypeAdapter.validate_python(first["messages"])
    assert all(isinstance(m, ModelRequest) for m in sent), "the request only: no reply was received"
    assert first["tools"] == retry["tools"] and first["finish_reason"] is None and first["request_hash"] != retry["request_hash"]
    (tmp_path / "runs" / rid / "trail.jsonl").unlink()
    with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:
        conn.execute("DELETE FROM failed_calls")
    rebuilt = trail(export(fake_client, rid))[0]
    assert (rebuilt["messages"], rebuilt["tools"], rebuilt["allow_text"], rebuilt["missing"]) == (None, None, None, "no_cache_row")


def test_the_trail_is_stored_when_the_run_finishes_and_the_export_serves_that_file(fake_client, tmp_path):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    stored = (tmp_path / "runs" / rid / "trail.jsonl").read_bytes()
    assert len(stored.splitlines()) == sum(e["type"] == "llm_call" for e in events)
    with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:
        conn.execute("DELETE FROM calls")
    assert export(fake_client, rid).read("trail.jsonl") == stored, "the stored file, not a rebuild from the pruned cache"


def test_a_cache_hit_from_another_run_names_the_run_that_paid(fake_client):
    fake_client.fake.brain = talker
    first = run_condition(fake_client, "C1")[0][0]["run_id"]
    second = run_condition(fake_client, "C1")[0][0]["run_id"]
    lines = trail(export(fake_client, second))
    assert lines and {(ln["cache_hit"], ln["charged"], ln["paid_by_run"]) for ln in lines} == {(True, False, first)}
    assert all(ln["messages"] and ln["missing"] is None for ln in lines)


def test_no_key_in_a_trail_or_a_zip(fake_client):
    fake_client.fake.brain = fail_first(talker)
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    resp = fake_client.get(f"/api/runs/{rid}/export", params={"format": "zip"})
    z = zipfile.ZipFile(BytesIO(resp.content))
    assert trail(z)[0]["error"] == "provider_error", "the scan covers a failed call"
    for blob in [resp.content, *(z.read(n) for n in z.namelist())]:
        assert KEY.encode() not in blob and not KEYLIKE.search(blob.decode("utf-8", "replace"))


def test_the_trail_is_a_sidecar_and_the_manifest_holds_the_sha256_of_every_file(fake_client):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    episode = fake_client.get(f"/api/runs/{rid}/episode.jsonl").content
    z = export(fake_client, rid)
    assert z.read("episode.jsonl") == episode == fake_client.get(f"/api/runs/{rid}/episode.jsonl").content
    _, stored = read_episode(episode.decode("utf-8").splitlines())
    assert b"trail.v1" not in episode and stored[-1].type == "run_ended", "the trail is not in the episode, so not in its hash"
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["manifest"] == "export-manifest.v1" and manifest["run_id"] == rid
    files = {f["path"]: f for f in manifest["files"]}
    assert sorted(files) == ["episode.jsonl", "meta.json", "trail.jsonl"]
    for path, f in files.items():
        data = z.read(path)
        assert (f["bytes"], f["sha256"]) == (len(data), "sha256:" + hashlib.sha256(data).hexdigest()), path
    raw = [fake_client.get(f"/api/runs/{rid}/export", params={"format": "zip"}).content for _ in range(2)]
    assert raw[0] == raw[1], "two exports of one run are the same zip bytes"
    assert {i.date_time for i in z.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_the_experiment_export_holds_every_started_run_with_its_trail_and_a_manifest(fake_client):
    exp = created(fake_client, body())
    detail = finished(fake_client, exp["id"])
    resp = fake_client.get(f"/api/experiments/{exp['id']}/export", params={"format": "zip"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(BytesIO(resp.content))
    runs = [c["parent"]["run_id"] for c in detail["cells"]] + [b["run_id"] for c in detail["cells"] for b in c["branches"]]
    assert len(runs) > 1, "the default plan has branch children"
    want = ["experiment.json"] + [f"runs/{r}/{f}" for r in runs for f in ("episode.jsonl", "meta.json", "trail.jsonl")]
    assert z.namelist() == [*want, "manifest.json"]
    assert json.loads(z.read("experiment.json"))["id"] == exp["id"]
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["experiment_id"] == exp["id"] and [f["path"] for f in manifest["files"]] == sorted(want)
    for f in manifest["files"]:
        assert f["sha256"] == "sha256:" + hashlib.sha256(z.read(f["path"])).hexdigest()
    for r in runs:
        assert z.read(f"runs/{r}/episode.jsonl") == fake_client.get(f"/api/runs/{r}/episode.jsonl").content
    missing = check(
        fake_client.get("/api/experiments/exp_01J000000000000000000000ZZ/export", params={"format": "zip"}),
        "/experiments/{experiment_id}/export",
    )
    assert missing["code"] == "not_found"


def test_the_trail_of_a_branch_export_is_under_its_branch(fake_client):
    exp = created(fake_client, body())
    detail = finished(fake_client, exp["id"])
    parent = detail["cells"][0]["parent"]["run_id"]
    children = [b["run_id"] for b in detail["cells"][0]["branches"]]
    names = export(fake_client, parent, include="branches").namelist()
    assert {f"branches/{c}/trail.jsonl" for c in children} <= set(names) and names[-1] == "manifest.json"


def test_a_cache_row_from_before_the_finish_reason_gives_null(fake_client, tmp_path):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    (tmp_path / "runs" / events[0]["run_id"] / "trail.jsonl").unlink()  # an older run: the export builds it again
    with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:
        rows = conn.execute("SELECT key, reply_json FROM calls").fetchall()
        assert rows and all("finish_reason" in json.loads(r) for _, r in rows), "new rows keep the finish reason"
        for key, reply in rows:
            old = {k: v for k, v in json.loads(reply).items() if k != "finish_reason"}
            conn.execute("UPDATE calls SET reply_json = ? WHERE key = ?", (json.dumps(old, sort_keys=True), key))
    lines = trail(export(fake_client, events[0]["run_id"]))
    assert lines and {ln["finish_reason"] for ln in lines} == {None}
    assert {ModelMessagesTypeAdapter.validate_python(ln["messages"])[-1].finish_reason for ln in lines} == {None}


def test_a_trail_error_never_keeps_a_run_from_its_ledger_line(fake_client, tmp_path, monkeypatch):
    import adapters.trail
    import services.read

    def broken(events, cache_path):
        raise OSError("disk full")

    monkeypatch.setattr(adapters.trail, "trail_text", broken)  # store imports it at call time
    monkeypatch.setattr(services.read, "trail_text", broken)  # the run folder builds it again (DECISIONS-20)
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    folder = tmp_path / "runs" / rid
    assert fake_client.app.state.db.ledger(rid) and (folder / "episode.jsonl").is_file(), "the run finished"
    assert not (folder / "trail.jsonl").exists() and not (folder / "trail.jsonl.tmp").exists()
    assert not (folder / "manifest.json").exists(), "no manifest: the folder is not a whole bundle"
    monkeypatch.undo()
    lines = trail(export(fake_client, rid))
    assert lines and {ln["missing"] for ln in lines} == {None}, "the export builds the trail again"


def test_a_damaged_row_or_an_unreadable_cache_hides_only_what_it_must(fake_client, tmp_path):
    from adapters.trail import trail_lines
    from simschema.events import SIM_EVENT

    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    (tmp_path / "runs" / rid / "trail.jsonl").unlink()
    first = next(e for e in events if e["type"] == "llm_call")
    with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:
        conn.execute("UPDATE calls SET request_json = '{not json' WHERE key = ?", (first["request_hash"],))
    lines = trail(export(fake_client, rid))
    bad = [ln for ln in lines if ln["missing"] == "unreadable_row"]
    assert [ln["seq"] for ln in bad] == [first["seq"]] and bad[0]["messages"] is None
    assert all(ln["messages"] for ln in lines if ln["seq"] != first["seq"]), "the other lines stay"
    parsed = [SIM_EVENT.validate_python(e) for e in events]
    (tmp_path / "folder.sqlite").mkdir()
    assert {ln["missing"] for ln in trail_lines(parsed, tmp_path / "folder.sqlite")} == {"no_cache_row"}
