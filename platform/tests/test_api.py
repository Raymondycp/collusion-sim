"""API unit 7a: every response checked against docs/sim/api/openapi.yaml; scripted runs only (no model call)."""

import json
import logging
import re
import shutil
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from starlette.websockets import WebSocketDisconnect

from api.app import create_app

ROOT = Path(__file__).parents[1]
SPEC = yaml.safe_load((ROOT / "docs" / "sim" / "api" / "openapi.yaml").read_text(encoding="utf-8"))
SCENARIO = json.loads((ROOT / "scenarios" / "scn_mock_demo.json").read_text(encoding="utf-8"))
KEYLIKE = re.compile(r"sk-[A-Za-z0-9_-]{8,}|api[_-]?key\"\s*:\s*\"[^\"]")


def schema_for(path: str, method: str, status: int) -> dict | None:
    resp = SPEC["paths"][path][method]["responses"].get(str(status))
    if resp is None:
        pytest.fail(f"{method.upper()} {path} returned {status}, which the contract does not list")
    if "$ref" in resp:
        return {"$ref": "#/components/schemas/Problem"}
    content = resp.get("content", {})
    media = content.get("application/json") or content.get("application/problem+json")
    return media["schema"] if media else None


def _resolve(node: dict) -> dict:
    while "$ref" in node:
        kind, name = node["$ref"].split("/")[-2:]
        node = SPEC["components"][kind][name]
    return node


def check_request(resp, path: str, method: str) -> None:
    """The test's own request must match the contract: only declared query params, and (when accepted) a valid body."""
    op = SPEC["paths"][path][method]
    params = [_resolve(p) for p in SPEC["paths"][path].get("parameters", []) + op.get("parameters", [])]
    declared = {p["name"] for p in params if p["in"] == "query"}
    sent = {k for k, _ in resp.request.url.params.multi_items()}
    assert sent <= declared, f"{method.upper()} {path}: query params {sorted(sent - declared)} are not in the contract"
    body = resp.request.content
    if body and 200 <= resp.status_code < 300 and "requestBody" in op:
        schema = _resolve(op["requestBody"])["content"]["application/json"]["schema"]
        Draft202012Validator({**schema, "components": SPEC["components"]}).validate(json.loads(body))


def check(resp, path: str, method: str = "get"):
    """Validate the request and the response body against the contract, and check that the response has no key."""
    check_request(resp, path, method)
    assert not KEYLIKE.search(resp.text), f"key-like text in {path}"
    schema = schema_for(path, method, resp.status_code)
    if schema is not None and resp.headers.get("content-type", "").startswith("application/"):
        Draft202012Validator({**schema, "components": SPEC["components"]}).validate(resp.json())
    return (
        resp.json() if resp.headers.get("content-type", "").startswith("application/") and "json" in resp.headers["content-type"] else resp
    )


def config(seeds=(1,), **kw):
    grants = [{"tool_id": t} for t in ("get_news", "get_own_sales", "set_price", "hold")]
    grants += [
        {"tool_id": "get_rival_prices", "params": {"depth": 5}},
        {"tool_id": "read_channel", "params": {"channels": ["private_A_B"]}},
        {"tool_id": "post", "params": {"channels": ["private_A_B"]}},
    ]
    cap = {"grants": grants, "max_queries_per_round": 3}
    body = {
        "kind": "exploratory",
        "name": "api test",
        "scenario_id": SCENARIO["id"],
        "scenario_hash": SCENARIO["hash"],
        "seeds": list(seeds),
        "agents": [
            {
                "id": "A",
                "role": "seller",
                "policy": {"kind": "scripted_talker", "params": {"price": 10.5, "offer_text": "Hold 10.5.", "channel_id": "private_A_B"}},
                "capability": cap,
            },
            {
                "id": "B",
                "role": "seller",
                "policy": {"kind": "grim_trigger", "params": {"target": 10.5, "punish_rounds": 3}},
                "capability": cap,
            },
        ],
        "channels": {"private_pairs": True},
        "call_mode": "talk_and_act",
        "clock": {"mode": "sync"},
        "budget": {"max_usd": 1.0},
    }
    return {**body, **kw}


@pytest.fixture
def client(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    with TestClient(create_app(tmp_path, use_dbos=False)) as c:
        yield c


def start(client, **kw) -> list[str]:
    body = check(client.post("/api/runs", json=config(**kw)), "/runs", "post")
    for rid in body["run_ids"]:
        client.app.state.svc.wait(rid, timeout=30)
    return body["run_ids"]


def test_registries_match_contract(client):
    health = check(client.get("/api/health"), "/health")
    assert health["durable_backend"] == "none" and health["api_version"] == SPEC["info"]["version"]
    for path in ("/roles", "/tools", "/policies", "/indicators", "/demand-models", "/scenarios"):
        assert check(client.get("/api" + path), path), path
    assert {"llm", "grim_trigger", "scripted_talker"} <= {p["kind"] for p in client.get("/api/policies").json()}
    check(client.get(f"/api/scenarios/{SCENARIO['id']}"), "/scenarios/{scenario_id}")
    missing = check(client.get("/api/scenarios/scn_nope"), "/scenarios/{scenario_id}")
    assert missing["code"] == "not_found"


def test_demand_registry_solver_and_scenario_reference(client):
    models = check(client.get("/api/demand-models"), "/demand-models")
    linear = next(m for m in models if m["id"] == "linear")
    assert linear["research_allowed"] and "a" in linear["params_schema"]["properties"] and linear["solver_schema"]
    solved = check(
        client.post("/api/demand-models/linear/solve", json={"nash": 5.75, "joint_optimum": 10.5, "b": 1.5, "c": 1.0}),
        "/demand-models/{model_id}/solve",
        "post",
    )
    assert solved["params"]["a"] == 10 and solved["params"]["d"] == 1 and solved["problems"] == []
    bad = check(
        client.post("/api/demand-models/linear/solve", json={"nash": 10.5, "joint_optimum": 5.75}),
        "/demand-models/{model_id}/solve",
        "post",
    )
    assert bad["code"] == "validation_error"
    assert check(client.post("/api/demand-models/logit/solve", json={}), "/demand-models/{model_id}/solve", "post")["status"] == 404

    series = check(client.get(f"/api/scenarios/{SCENARIO['id']}/reference"), "/scenarios/{scenario_id}/reference")
    by_t = {r["t"]: r for r in series["rounds"]}
    assert len(by_t) == 30 and by_t[9]["nash"] == 5.75 and by_t[10]["nash"] == 6.125 and by_t[15]["nash"] == 5.75
    assert by_t[10]["costs"] == {"A": 1.5, "B": 1.5}
    assert [(m["t"], m["kind"]) for m in series["markers"]] == [(8, "announce"), (10, "effect"), (15, "end")]
    assert (
        len(
            check(client.get(f"/api/scenarios/{SCENARIO['id']}/reference", params={"until": 5}), "/scenarios/{scenario_id}/reference")[
                "rounds"
            ]
        )
        == 5
    )


def test_generate_returns_a_checked_draft_and_saves_nothing(client):
    gens = check(client.get("/api/scenario-generators"), "/scenario-generators")
    assert {"template", "random"} <= {g["id"] for g in gens} and not any(g["paid"] for g in gens)
    before = client.get("/api/scenarios").json()
    body = check(
        client.post("/api/scenarios/generate", json={"generator_id": "random", "inputs": {"n_events": 2}, "seed": 4}),
        "/scenarios/generate",
        "post",
    )
    assert body["check"]["ok"] and body["draft"]["provenance"]["seed"] == 4 and "hash" not in body["draft"]
    again = client.post("/api/scenarios/generate", json={"generator_id": "random", "inputs": {"n_events": 2}, "seed": 4}).json()
    assert again["draft"] == body["draft"]
    assert client.get("/api/scenarios").json() == before
    assert (
        check(client.post("/api/scenarios/generate", json={"generator_id": "nope", "inputs": {}}), "/scenarios/generate", "post")["code"]
        == "unknown_generator"
    )
    bad = check(
        client.post("/api/scenarios/generate", json={"generator_id": "template", "inputs": {"template": "nope"}}),
        "/scenarios/generate",
        "post",
    )
    assert bad["code"] == "validation_error" and bad["errors"][0]["path"].startswith("inputs/")


def test_a_scenario_id_cannot_leave_the_scenarios_folder(client, tmp_path):
    shutil.copy(tmp_path / "scenarios" / "scn_mock_demo.json", tmp_path / "outside.json")
    resp = client.get("/api/scenarios/scn_x%5C..%5C..%5Coutside")
    assert resp.status_code == 404 and resp.json()["code"] == "not_found"
    assert client.get("/api/scenarios/scn_mock_demo").status_code == 200


def test_scenario_draft_edit_check_freeze_flow(client, tmp_path):
    gen = client.post("/api/scenarios/generate", json={"generator_id": "template", "inputs": {"template": "cost_shock_public"}}).json()
    created = check(client.post("/api/scenarios", json=gen["draft"]), "/scenarios", "post")
    sid = created["id"]
    assert sid.startswith("scn_")
    assert created["status"] == "draft" and created["version"] == 1 and created["provenance"]["generator_id"] == "template"
    assert sid in {s["id"] for s in check(client.get("/api/scenarios", params={"status": "draft"}), "/scenarios")}

    broken = {**gen["draft"], "events": [{**gen["draft"]["events"][0], "news": "Wholesale costs fall."}]}
    edited = check(client.put(f"/api/scenarios/{sid}", json=broken), "/scenarios/{scenario_id}", "put")
    assert edited["version"] == 2
    report = check(client.post(f"/api/scenarios/{sid}/check"), "/scenarios/{scenario_id}/check", "post")
    assert not report["ok"] and {c["id"] for c in report["checks"] if not c["ok"]} == {"news_direction"}
    failed = check(client.post(f"/api/scenarios/{sid}/freeze", json={"approved_by": "lead"}), "/scenarios/{scenario_id}/freeze", "post")
    assert failed["code"] == "scenario_check_failed" and failed["errors"][0]["path"] == "checks/news_direction"
    assert check(client.get(f"/api/scenarios/{sid}/reference", params={"until": 9}), "/scenarios/{scenario_id}/reference")["hash"] is None
    run = check(client.post("/api/runs", json=config(scenario_id=sid, scenario_hash="sha256:" + "0" * 64)), "/runs", "post")
    assert run["code"] == "scenario_not_frozen"

    check(client.put(f"/api/scenarios/{sid}", json=gen["draft"]), "/scenarios/{scenario_id}", "put")
    frozen = check(client.post(f"/api/scenarios/{sid}/freeze", json={"approved_by": "lead"}), "/scenarios/{scenario_id}/freeze", "post")
    assert frozen["status"] == "frozen" and frozen["hash"].startswith("sha256:") and frozen["approved_by"] == "lead"
    assert (tmp_path / "scenarios" / f"{sid}.json").exists()
    assert check(client.put(f"/api/scenarios/{sid}", json=gen["draft"]), "/scenarios/{scenario_id}", "put")["code"] == "scenario_frozen"
    assert check(client.delete(f"/api/scenarios/{sid}"), "/scenarios/{scenario_id}", "delete")["code"] == "scenario_frozen"
    (rid,) = start(client, scenario_id=sid, scenario_hash=frozen["hash"])
    assert client.get(f"/api/runs/{rid}").json()["status"] == "completed"

    other = check(client.post("/api/scenarios", json=gen["draft"]), "/scenarios", "post")["id"]
    assert client.delete(f"/api/scenarios/{other}").status_code == 204
    assert check(client.get(f"/api/scenarios/{other}"), "/scenarios/{scenario_id}")["code"] == "not_found"
    bad = check(client.post("/api/scenarios", json={**gen["draft"], "hash": "sha256:x"}), "/scenarios", "post")
    assert bad["code"] == "validation_error" and bad["errors"][0]["path"] == "hash"


def test_market_preview_is_pure_calculation(client):
    body = {
        "market": {"demand": {"id": "linear", "params": {"a": 10, "b": 1.5, "d": 1.0, "c": 1.0}}},
        "n_sellers": 2,
        "rounds": 6,
        "events": [
            {
                "announce": 2,
                "effect": 3,
                "length": 2,
                "shocks": [{"path": "/c", "op": "add", "value": 0.5}],
                "visible": ["A"],
                "news": "Costs rise.",
            }
        ],
    }
    out = check(client.post("/api/market/preview", json=body), "/market/preview", "post")
    assert [r["nash"] for r in out["rounds"]] == [5.75, 5.75, 6.125, 6.125, 5.75, 5.75]
    assert all(c["ok"] for c in out["checks"]) and "horizon" not in {c["id"] for c in out["checks"]}
    bad = {**body, "events": [{**body["events"][0], "shocks": [{"path": "/zz", "op": "add", "value": 1}]}]}
    out = check(client.post("/api/market/preview", json=bad), "/market/preview", "post")
    assert {c["id"] for c in out["checks"] if not c["ok"]} == {"shock_paths", "stability"}
    assert [r["t"] for r in out["rounds"]] == [1, 2, 5, 6]


def test_docs_serve_our_contract(client):
    assert "swagger" in client.get("/api/docs").text.lower()
    assert client.get("/api/openapi.json").json()["info"]["version"] == SPEC["info"]["version"]


def test_scripted_run_read_side(client):
    (rid,) = start(client)
    detail = check(client.get(f"/api/runs/{rid}"), "/runs/{run_id}")
    assert detail["status"] == "completed" and type(detail["t"]) is int and detail["t"] == 30 and detail["max_rounds"] == 30
    listing = check(client.get("/api/runs"), "/runs")
    assert [r["id"] for r in listing["items"]] == [rid]

    page = check(client.get(f"/api/runs/{rid}/events", params={"limit": 50}), "/runs/{run_id}/events")
    assert [e["seq"] for e in page["events"]] == list(range(50)) and page["next_from_seq"] == 50
    posts = check(client.get(f"/api/runs/{rid}/events", params={"types": "posted", "agent_id": "A"}), "/runs/{run_id}/events")
    assert len(posts["events"]) == 30 and {e["type"] for e in posts["events"]} == {"posted"}

    series = check(client.get(f"/api/runs/{rid}/series"), "/runs/{run_id}/series")
    assert series["t"] == list(range(1, 31)) and set(series["sellers"]["B"]["price"]) == {10.5}
    assert {m["kind"] for m in series["markers"]} >= {"news", "post"}

    state = check(client.get(f"/api/runs/{rid}/state", params={"t": 12}), "/runs/{run_id}/state")
    assert state["t"] == 12 and state["sellers"]["A"]["price"] == 10.5
    assert check(client.get(f"/api/runs/{rid}/state", params={"t": 99}), "/runs/{run_id}/state")["code"] == "snapshot_missing"

    obs = check(client.get(f"/api/runs/{rid}/observations", params={"agent_id": "A", "t": 3}), "/runs/{run_id}/observations")
    assert obs["calls"] == []

    lines = client.get(f"/api/runs/{rid}/episode.jsonl").text.splitlines()
    assert json.loads(lines[0])["run_id"] == rid and json.loads(lines[-1])["type"] == "run_ended"
    csv_text = client.get(f"/api/runs/{rid}/export", params={"format": "csv_rounds"}).text
    assert len(csv_text.splitlines()) == 1 + 30 * 2
    z = zipfile.ZipFile(BytesIO(client.get(f"/api/runs/{rid}/export", params={"format": "zip"}).content))
    assert z.namelist() == ["episode.jsonl", "meta.json", "trail.jsonl", "manifest.json"]
    assert z.read("trail.jsonl") == b"", "scripted sellers make no LLM call"
    assert check(client.get(f"/api/runs/{rid}/export", params={"format": "sandbox_json"}), "/runs/{run_id}/export")["status"] == 422


def test_indicator_instance(client):
    (rid,) = start(client)
    path = "/runs/{run_id}/indicators/{indicator_id}"
    inst = check(client.get(f"/api/runs/{rid}/indicators/delta_p", params={"until": 10}), path)
    assert inst["t"] == list(range(1, 11)) and inst["run_id"] == rid
    bad = check(client.get(f"/api/runs/{rid}/indicators/corr_raw", params={"params": json.dumps({"window": 99})}), path)
    assert bad["code"] == "indicator_params_invalid" and bad["errors"]
    assert check(client.get(f"/api/runs/{rid}/indicators/nope"), path)["status"] == 404


def test_branch_and_ledger(client):
    (rid,) = start(client)
    body = check(
        client.post(
            f"/api/runs/{rid}/branches",
            json={"at": 15, "rounds": 5, "n": 2, "intervention": {"type": "probe", "target": "A", "price": "nash", "rounds": 1}},
        ),
        "/runs/{run_id}/branches",
        "post",
    )
    for child in body["run_ids"]:
        client.app.state.svc.wait(child, timeout=30)
    kids = check(client.get(f"/api/runs/{rid}/branches"), "/runs/{run_id}/branches")
    assert sorted(k["id"] for k in kids) == sorted(body["run_ids"]) and all(k["branch"]["parent_run_id"] == rid for k in kids)
    child = kids[0]["id"]
    b = client.get(f"/api/runs/{child}/series").json()["sellers"]["B"]["price"]
    assert b[0] == 10.5 and b[1] < 10.5
    late = check(
        client.post(
            f"/api/runs/{rid}/branches",
            json={"at": 40, "rounds": 5, "n": 1, "intervention": {"type": "probe", "target": "A", "price": "nash", "rounds": 1}},
        ),
        "/runs/{run_id}/branches",
        "post",
    )
    assert (late["status"], late["code"], [e["path"] for e in late["errors"]]) == (409, "snapshot_missing", ["at"])
    for iv, path in [
        ({"type": "probe", "target": "Z", "price": "nash", "rounds": 1}, "intervention/target"),
        ({"type": "probe", "target": "A", "price": "nash", "rounds": 9}, "intervention/rounds"),
        ({"type": "ablation", "channel_id": "public", "mode": "clear"}, "intervention/channel_id"),
    ]:
        bad = check(
            client.post(f"/api/runs/{rid}/branches", json={"at": 15, "rounds": 5, "n": 1, "intervention": iv}),
            "/runs/{run_id}/branches",
            "post",
        )
        assert (bad["status"], bad["code"], [e["path"] for e in bad["errors"]]) == (422, "validation_error", [path])
    assert check(client.post(f"/api/runs/{rid}/branches", json={"at": 15}), "/runs/{run_id}/branches", "post")["code"] == "validation_error"

    ledger = check(client.get("/api/ledger"), "/ledger")
    assert len(ledger["items"]) == 3 and ledger["items"][0]["run_id"] == rid
    assert check(client.get("/api/ledger/verify"), "/ledger/verify") == {"ok": True, "entries": 3, "first_bad_line": None}


def test_errors_are_problems(client):
    research = check(client.post("/api/runs", json=config(kind="research", split="dev", condition_id="C1")), "/runs", "post")
    assert research["status"] == 422
    wrong_hash = check(client.post("/api/runs", json=config(scenario_hash="sha256:" + "0" * 64)), "/runs", "post")
    assert wrong_hash["code"] == "hash_mismatch"
    shape = check(client.post("/api/runs", json={"kind": "exploratory"}), "/runs", "post")
    assert shape["code"] == "validation_error" and shape["errors"]
    assert check(client.get("/api/runs/run_nope"), "/runs/{run_id}")["code"] == "not_found"


def test_websocket_replay_and_indicators(client):
    (rid,) = start(client)
    with client.websocket_connect(f"/api/ws/runs/{rid}?from_seq=5") as ws:
        ws.send_json({"type": "subscribe", "subscriptions": [{"key": "dp", "id": "delta_p", "params": {}}]})
        msgs = []
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                msgs.append(ws.receive_json())
    assert closed.value.code == 1000
    assert not any(KEYLIKE.search(json.dumps(m)) for m in msgs)
    events = [m["event"] for m in msgs if m["type"] == "event"]
    assert events[0]["seq"] == 5 and events[-1]["type"] == "run_ended"
    assert [e["seq"] for e in events] == list(range(5, events[-1]["seq"] + 1))
    caught = [i for i, m in enumerate(msgs) if m["type"] == "caught_up"]
    assert len(caught) == 1 and msgs[caught[0]]["last_seq"] == events[-1]["seq"]
    updates = [m for m in msgs if m["type"] == "indicator_update"]
    assert [u["t"] for u in updates] == list(range(1, 31)) and updates[0]["key"] == "dp"
    ws_schema = SPEC["components"]["schemas"]["WsServerMessage"]
    validator = Draft202012Validator({**ws_schema, "components": SPEC["components"]})
    for m in msgs:
        validator.validate(m)


@pytest.mark.parametrize("url, code", [("/api/ws/runs/run_nope", 4404), ("/api/ws/runs/{rid}?from_seq=100000", 4409)])
def test_websocket_close_codes(client, url, code):
    (rid,) = start(client)
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(url.format(rid=rid)) as ws:
            ws.receive_json()
    assert closed.value.code == code


def test_malformed_client_json_closes_the_stream_with_1007(client, monkeypatch):
    import threading

    from simcore.scripted import GrimTrigger

    gate, act = threading.Event(), GrimTrigger.act

    def slow(self, obs, tools):
        if obs.t == 2:
            gate.wait(10)
        return act(self, obs, tools)

    monkeypatch.setattr(GrimTrigger, "act", slow)
    (rid,) = check(client.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
    try:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(f"/api/ws/runs/{rid}") as ws:
                ws.send_text("{not json")
                while True:
                    ws.receive_json()
        assert closed.value.code == 1007
    finally:
        gate.set()
    assert client.app.state.svc.wait(rid, timeout=30)["status"] == "completed"


def test_a_run_error_under_dbos_is_recorded_as_a_failed_run(tmp_path, monkeypatch):
    from simcore.scripted import GrimTrigger

    act = GrimTrigger.act

    def broken(self, obs, tools):
        if obs.t == 3:
            raise RuntimeError("boom with sk-live-0123456789abcdefXYZ in it")
        return act(self, obs, tools)

    logged = []
    handler = logging.Handler()
    handler.emit = lambda record: logged.append(record.getMessage())
    monkeypatch.setattr(logging.getLogger("sim.runner"), "handlers", [handler])
    monkeypatch.setattr(GrimTrigger, "act", broken)
    monkeypatch.setenv("SIM_DBOS_PATH", str(tmp_path / "data" / "dbos.sqlite"))
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    with TestClient(create_app(tmp_path, use_dbos=True)) as c:
        (rid,) = check(c.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
        assert c.app.state.svc.wait(rid, timeout=60)["status"] == "failed"
        run = check(c.get(f"/api/runs/{rid}"), "/runs/{run_id}")
        assert run["status"] == "failed"
        assert [e["run_id"] for e in check(c.get("/api/ledger"), "/ledger")["items"]] == [rid]
        ended = check(c.get(f"/api/runs/{rid}/events", params={"types": "run_ended"}), "/runs/{run_id}/events")["events"]
        assert [(e["status"], e["error"]) for e in ended] == [("failed", "RuntimeError")]
    assert logged == [f"run {rid} failed: RuntimeError: boom with [redacted] in it"]


def test_a_shutdown_leaves_a_live_dbos_run_pending_and_the_next_start_resumes_it(tmp_path, monkeypatch):
    import threading
    import time

    from adapters.store import RunsDB
    from simcore.scripted import GrimTrigger

    gate, act = threading.Event(), GrimTrigger.act

    def slow(self, obs, tools):
        if obs.t == 2:
            gate.wait(10)
        return act(self, obs, tools)

    monkeypatch.setattr(GrimTrigger, "act", slow)
    monkeypatch.setenv("SIM_DBOS_PATH", str(tmp_path / "data" / "dbos.sqlite"))
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    with TestClient(create_app(tmp_path, use_dbos=True)) as c:
        (rid,) = check(c.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
        check(c.post(f"/api/runs/{rid}/pause"), "/runs/{run_id}/pause", "post")
        gate.set()
        deadline = time.time() + 30
        while not any(e.type == "run_status_changed" and e.status == "paused" for e in c.app.state.db.events(rid)):
            assert time.time() < deadline, "the run never paused"
            time.sleep(0.05)
    db = RunsDB(tmp_path / "data" / "runs.sqlite")
    assert "run_ended" not in {e.type for e in db.events(rid)}
    with TestClient(create_app(tmp_path, use_dbos=True)) as c:
        assert c.app.state.svc.wait(rid, timeout=60)["status"] == "completed"
        events = check(c.get(f"/api/runs/{rid}/events", params={"types": "run_status_changed,run_ended"}), "/runs/{run_id}/events")[
            "events"
        ]
    assert [e.get("reason") for e in events if e["type"] == "run_status_changed" and e["status"] == "running"][-1] == "recovered"
    assert [(e["status"], e["end_reason"]) for e in events if e["type"] == "run_ended"] == [("completed", "horizon")]


def test_dbos_runs_at_most_four_runs_at_once(tmp_path, monkeypatch):
    import threading
    import time

    from adapters import durable

    monkeypatch.setenv("SIM_DBOS_PATH", str(tmp_path / "data" / "dbos.sqlite"))
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    lock, live, peak = threading.Lock(), [0], [0]
    with TestClient(create_app(tmp_path, use_dbos=True)) as c:
        execute = durable._ctx["execute"]

        def counted(run_id):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.5)
            try:
                return execute(run_id)
            finally:
                with lock:
                    live[0] -= 1

        monkeypatch.setitem(durable._ctx, "execute", counted)
        ids = check(c.post("/api/runs", json=config(seeds=range(1, 7))), "/runs", "post")["run_ids"]
        assert {c.app.state.svc.wait(rid, timeout=120)["status"] for rid in ids} == {"completed"}
    assert peak[0] == durable.MAX_PARALLEL_RUNS


def test_control_routes_take_no_hidden_action_parameter(client):
    import threading
    import time

    from simcore.scripted import GrimTrigger

    gate = threading.Event()
    act = GrimTrigger.act

    def slow(self, obs, tools):
        if obs.t == 2:
            gate.wait(10)
        return act(self, obs, tools)

    GrimTrigger.act = slow
    try:
        (rid,) = check(client.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
        paused = client.post(f"/api/runs/{rid}/pause", params={"_action": "stop"}).json()
        gate.set()
        for _ in range(100):
            if check(client.get(f"/api/runs/{rid}"), "/runs/{run_id}")["status"] == "paused":
                break
            time.sleep(0.05)
        assert check(client.get(f"/api/runs/{rid}"), "/runs/{run_id}")["status"] == "paused", paused
        check(client.post(f"/api/runs/{rid}/resume"), "/runs/{run_id}/resume", "post")
        assert client.app.state.svc.wait(rid, timeout=30)["status"] == "completed"
    finally:
        GrimTrigger.act = act


def test_stop_ends_a_live_run_and_control_of_an_ended_run_is_refused(client):
    import threading

    from simcore.scripted import GrimTrigger

    gate, act = threading.Event(), GrimTrigger.act

    def slow(self, obs, tools):
        if obs.t == 2:
            gate.wait(10)
        return act(self, obs, tools)

    GrimTrigger.act = slow
    try:
        (rid,) = check(client.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
        check(client.post(f"/api/runs/{rid}/stop"), "/runs/{run_id}/stop", "post")
        gate.set()
        assert client.app.state.svc.wait(rid, timeout=30)["status"] == "stopped"
    finally:
        GrimTrigger.act = act
    ended = check(client.get(f"/api/runs/{rid}/events", params={"types": "run_ended"}), "/runs/{run_id}/events")["events"]
    assert [(e["status"], e["end_reason"]) for e in ended] == [("stopped", "stopped_by_user")]
    for action in ("pause", "resume", "stop"):
        assert check(client.post(f"/api/runs/{rid}/{action}"), f"/runs/{{run_id}}/{action}", "post")["code"] == "run_not_active"


def test_every_contract_operation_is_served_or_answers_not_built(client):
    from api.app import NOT_BUILT

    served = {(m.lower(), r.path.removeprefix("/api")) for r in client.app.routes for m in getattr(r, "methods", None) or ()}
    contract = {(m, p) for p, ops in SPEC["paths"].items() for m in ops if m in ("get", "post", "put", "patch", "delete")}
    assert contract <= served and set(NOT_BUILT) <= contract
    for method, path in NOT_BUILT:
        resp = client.request(method.upper(), "/api" + path.replace("{", "x_").replace("}", ""))
        assert resp.status_code == 501 and resp.json()["code"] == "not_built", (method, path)
    compare = client.get("/api/runs/compare")
    assert compare.status_code == 501 and compare.headers["content-type"] == "application/problem+json"
    unknown = client.get("/api/no-such-path")
    assert unknown.status_code == 404 and unknown.json()["code"] == "not_found"


def test_live_indicator_updates_carry_values_for_the_newest_round(client, monkeypatch):
    import threading

    from simcore.scripted import GrimTrigger

    gate, act = threading.Event(), GrimTrigger.act

    def slow(self, obs, tools):
        if obs.t == 3:
            gate.wait(10)
        return act(self, obs, tools)

    monkeypatch.setattr(GrimTrigger, "act", slow)
    (rid,) = check(client.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
    updates = []
    try:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/api/ws/runs/{rid}") as ws:
                ws.send_json({"type": "subscribe", "subscriptions": [{"key": "dp", "id": "delta_p", "params": {}}]})
                while True:
                    m = ws.receive_json()
                    if m["type"] == "indicator_update":
                        updates.append(m)
                        gate.set()  # the subscription is active before rounds 3+ are played
    finally:
        gate.set()
    live = [u for u in updates if u["t"] >= 3]
    assert live and all(u["values"]["delta_p"] is not None for u in live)


def test_llm_is_the_first_policy_because_the_web_uses_it_as_the_default(client):
    assert check(client.get("/api/policies"), "/policies")[0]["kind"] == "llm"


def test_the_contract_rejects_an_event_field_the_engine_does_not_have():
    schema = {"$ref": "#/components/schemas/SimEvent", "components": SPEC["components"]}
    episode = (ROOT / "docs" / "sim" / "api" / "examples" / "episode-demo.jsonl").read_text(encoding="utf-8").splitlines()
    acted = next(json.loads(line) for line in episode if json.loads(line).get("type") == "acted")
    Draft202012Validator(schema).validate(acted)
    assert not Draft202012Validator(schema).is_valid({**acted, "plan": "Hold the high price."})


def test_a_shutdown_during_a_long_round_stores_that_round_before_the_writer_closes(tmp_path, monkeypatch):
    import threading
    import time

    from adapters.store import RunsDB
    from simcore.scripted import GrimTrigger

    entered, act = threading.Event(), GrimTrigger.act

    def slow(self, obs, tools):
        if obs.t == 3 and not entered.is_set():
            entered.set()
            time.sleep(6.0)  # longer than the 5 s DBOS waits at shutdown
        return act(self, obs, tools)

    monkeypatch.setattr(GrimTrigger, "act", slow)
    monkeypatch.setenv("SIM_DBOS_PATH", str(tmp_path / "data" / "dbos.sqlite"))
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    with TestClient(create_app(tmp_path, use_dbos=True)) as c:
        (rid,) = check(c.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
        assert entered.wait(30)
    stored = RunsDB(tmp_path / "data" / "runs.sqlite").events(rid)
    assert 3 in {e.t for e in stored if e.type == "settled"} and "run_ended" not in {e.type for e in stored}
    with TestClient(create_app(tmp_path, use_dbos=True)) as c:
        assert c.app.state.svc.wait(rid, timeout=60)["status"] == "completed"
