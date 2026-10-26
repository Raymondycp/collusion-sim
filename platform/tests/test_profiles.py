"""Provider profiles and keys: a key goes in once, is stored server side, and never comes back (only a masked hint)."""

import json
import shutil
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from adapters.keys import KeyStore, hint, key_env, read_env_file
from api.app import create_app
from fakellm import FakeLLM, call
from services.configs import ApiError
from services.profiles import probe_profile
from test_api import ROOT, check

KEY = "sk-test-0123456789abcdef"
NEW_KEY = "sk-new-9876543210zyxw"


class MemoryKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        del self.data[(service, username)]


@pytest.fixture
def ring():
    return MemoryKeyring()


@pytest.fixture
def client(tmp_path, ring, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    with TestClient(create_app(tmp_path, use_dbos=False, key_backend=ring)) as c:
        yield c


def profile_body(**kw):
    return {
        "name": "Local test",
        "base_url": "https://llm.example.test/v1",
        "api_key": KEY,
        "default_model": "m-small",
        "pricing": {"input_usd_per_mtok": 0.1, "output_usd_per_mtok": 0.4},
        **kw,
    }


def no_key_anywhere(root: Path, *texts: str):
    for t in texts:
        assert KEY not in t and NEW_KEY not in t
    for f in root.rglob("*"):
        if f.is_file() and f.name != ".env":
            raw = f.read_bytes()
            assert KEY.encode() not in raw and NEW_KEY.encode() not in raw, f


def test_create_stores_the_key_in_the_keyring_and_returns_only_a_hint(client, ring, tmp_path):
    resp = client.post("/api/provider-profiles", json=profile_body())
    created = check(resp, "/provider-profiles", "post")
    pid = created["id"]
    assert pid.startswith("prof_") and created["key_env"].startswith("SIM_KEY_PROF_")
    assert created["key_set"] and created["key_store"] == "keyring" and created["key_hint"] == "sk-…cdef"
    assert created["key_env"] == key_env(pid) and "api_key" not in created
    assert ring.data == {("collusion-sim", pid): KEY}
    listing = client.get("/api/provider-profiles")
    one = client.get(f"/api/provider-profiles/{pid}")
    check(listing, "/provider-profiles")
    check(one, "/provider-profiles/{profile_id}")
    no_key_anywhere(tmp_path, resp.text, listing.text, one.text)


def test_patch_replaces_the_key_and_a_bad_body_does_not_echo_it(client, ring, tmp_path):
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    patched = check(
        client.patch(f"/api/provider-profiles/{pid}", json={"api_key": NEW_KEY, "default_model": "m-large"}),
        "/provider-profiles/{profile_id}",
        "patch",
    )
    assert patched["key_hint"] == "sk-…zyxw" and patched["default_model"] == "m-large" and ring.data[("collusion-sim", pid)] == NEW_KEY
    bad = client.post("/api/provider-profiles", json=profile_body(name="", base_url="ftp://x", api_key=KEY))
    assert check(bad, "/provider-profiles", "post")["code"] == "validation_error"
    wrong_type = client.patch(f"/api/provider-profiles/{pid}", json={"max_concurrency": KEY})
    assert check(wrong_type, "/provider-profiles/{profile_id}", "patch")["code"] == "validation_error"
    no_key_anywhere(tmp_path, bad.text, wrong_type.text)


def test_env_file_is_a_read_only_fallback(client, ring, tmp_path):
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    ring.data.clear()
    assert client.get(f"/api/provider-profiles/{pid}").json()["key_set"] is False
    (tmp_path / ".env").write_text(f'# team keys\nexport {key_env(pid)}="{KEY}"\nOTHER=1\n', encoding="utf-8")
    view = client.get(f"/api/provider-profiles/{pid}").json()
    assert view["key_set"] and view["key_store"] == "env" and view["key_hint"] == hint(KEY)
    before = (tmp_path / ".env").read_bytes()
    assert check(client.delete(f"/api/provider-profiles/{pid}"), "/provider-profiles/{profile_id}", "delete").status_code == 204
    assert (tmp_path / ".env").read_bytes() == before


def test_delete_removes_the_keyring_entry(client, ring):
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    client.delete(f"/api/provider-profiles/{pid}")
    assert ring.data == {} and client.get(f"/api/provider-profiles/{pid}").status_code == 404


def test_test_call_is_refused_while_paid_calls_are_off(client):
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    refused = check(client.post(f"/api/provider-profiles/{pid}/test", json={}), "/provider-profiles/{profile_id}/test", "post")
    assert refused["code"] == "paid_calls_disabled"


def test_test_call_detects_tool_support_on_a_fake_endpoint(tmp_path, ring):
    from services.profiles import ProfileService

    profiles = ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    pid = profiles.create("prof_t", profile_body())["id"]
    llm = FakeLLM(lambda body: call("ping", ok=True))
    result = probe_profile(profiles, pid, None, transport=llm.transport())
    assert result["ok"] and result["supports"]["tool_calls"] is True and result["model"] == "m-small"
    assert profiles.get(pid)["supports"]["tool_calls"] is True
    failing = probe_profile(profiles, pid, None, transport=FakeLLM(lambda body: 401).transport())
    assert failing["error"]["kind"] == "auth" and KEY not in json.dumps(failing)
    profiles.close()


def test_a_failed_test_call_keeps_the_provider_code_status_and_a_redacted_message(tmp_path, ring):
    """Lead, 23 Oct: the test result says why the call failed, never with the key or the request body."""
    from services.profiles import ProfileService

    def gone(request):
        return httpx2.Response(410, json={"error": {"message": f"Model m-small is gone; key {KEY} rejected", "type": "model_gone"}})

    profiles = ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    pid = profiles.create("prof_t", profile_body())["id"]
    error = probe_profile(profiles, pid, None, transport=httpx2.MockTransport(gone))["error"]
    assert (error["http_status"], error["provider_code"]) == (410, "model_gone")  # the kind of a 410 is de/d22's (CR-90)
    assert error["provider_message"] == "Model m-small is gone; key [redacted] rejected"
    assert KEY not in json.dumps(error) and "Call the ping tool" not in json.dumps(error)
    profiles.close()


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        (
            "Incorrect API key provided: sk-proj-ABCD**************************wxyz",
            "Incorrect API key provided: [redacted]",
        ),
        ("Key sk-…cdef is not valid for this plan", "Key [redacted] is not valid for this plan"),
        ("Token AbCdEfGhIjKlMnOpQrStUvWxYz012345 expired", "Token [redacted] expired"),
        ("The model minimax-m3.1-flash-free has been removed", "The model minimax-m3.1-flash-free has been removed"),
    ],
)
def test_the_provider_message_masks_keys_and_their_echoes_but_keeps_the_model_id(tmp_path, ring, said, shown):
    """CR-89 addendum 1: a masked echo, the stored key's hint and a long bare token go; a dotted, hyphenated model id stays."""
    from services.profiles import ProfileService

    profiles = ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    pid = profiles.create("prof_t", profile_body())["id"]
    reply = httpx2.MockTransport(lambda request: httpx2.Response(401, json={"error": {"message": said, "code": "invalid_api_key"}}))
    error = probe_profile(profiles, pid, None, transport=reply)["error"]
    assert (error["kind"], error["provider_message"]) == ("auth", shown)
    profiles.close()


STORED = "AIzaSyA-12345_xyzABCDEFGHijklmnop"


@pytest.mark.parametrize(
    "said",
    [
        "Incorrect API key provided: sk-proj-ABCD**************************wxyz",
        "Incorrect API key provided: sk-proj-ABCD\u2026wxyz",
    ],
)
def test_a_masked_echo_of_the_stored_sk_key_is_removed_whole(said):
    """CR-89 addendum 3 point 1: the stored key is an sk-proj key and the provider echoes it masked."""
    from adapters.keys import mask_secrets

    stored = "sk-proj-ABCDEFGH1234567890ijklmnopqrstuvwxyz"
    assert mask_secrets(said, (stored,)) == "Incorrect API key provided: [redacted]"


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        ("Incorrect API key provided: AIzaSyA-1234...", "Incorrect API key provided: [redacted]"),  # first 12
        ("key ending 5_xyzABCDEFGH was refused", "key ending [redacted] was refused"),  # middle 12
        ("Incorrect API key provided: sk-proj-ABCD…wxyz", "Incorrect API key provided: [redacted]"),
        ("risk-assessment-model is busy", "risk-assessment-model is busy"),
        ("Key AIzaSyB-1234567890_abcdefghijklmnopqrstuvw is invalid", "Key [redacted] is invalid"),
        ("Bad token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123", "Bad token [redacted]"),
        ("claude-sonnet-5-5-20261001 and org/model-name-v2.5 are listed", "claude-sonnet-5-5-20261001 and org/model-name-v2.5 are listed"),
    ],
)
def test_mask_secrets_removes_parts_of_the_stored_key_and_other_key_shapes_but_not_names(said, shown):
    """CR-89 addendum 2 points 1 to 3."""
    from adapters.keys import mask_secrets

    assert mask_secrets(said, (STORED,)) == shown


def test_the_route_passes_thinking_and_refuses_an_unknown_level(client, monkeypatch):
    import services.profiles as profiles_svc

    monkeypatch.setenv("SIM_ALLOW_PAID", "1")
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    seen = []
    monkeypatch.setattr(
        profiles_svc, "probe_profile", lambda profiles, p, model, transport=None, thinking=None: seen.append((model, thinking)) or {}
    )
    assert client.post(f"/api/provider-profiles/{pid}/test", json={"model": "m", "thinking": False}).status_code == 200
    assert client.post(f"/api/provider-profiles/{pid}/test", json={"thinking": "low"}).status_code == 200
    assert client.post(f"/api/provider-profiles/{pid}/test", json={"thinking": "none"}).json()["code"] == "validation_error"
    for bad in (0, 1):  # CR-89 addendum 2 point 4: 0 == False and 1 == True in Python
        assert client.post(f"/api/provider-profiles/{pid}/test", json={"thinking": bad}).json()["code"] == "validation_error"
    assert seen == [("m", False), (None, "low")]


def test_the_test_call_sends_no_output_limit_and_the_thinking_it_is_given(tmp_path, ring):
    from services.profiles import ProfileService

    profiles = ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    pid = profiles.create("prof_t", profile_body())["id"]
    llm = FakeLLM(lambda body: call("ping", ok=True))
    assert probe_profile(profiles, pid, None, transport=llm.transport(), thinking="low")["ok"]
    assert probe_profile(profiles, pid, None, transport=llm.transport(), thinking=False)["ok"]
    assert probe_profile(profiles, pid, None, transport=llm.transport())["ok"]
    sent = llm.bodies
    assert [b.get("reasoning_effort") for b in sent] == ["low", "none", None]
    assert not any("max_tokens" in b or "max_completion_tokens" in b for b in sent)
    profiles.close()


def test_models_lists_ids_only(tmp_path, ring):
    from services.profiles import ProfileService

    profiles = ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    pid = profiles.create("prof_m", profile_body())["id"]
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx2.Response(200, json={"data": [{"id": "m-b", "owned_by": "x"}, {"id": "m-a"}]})

    assert profiles.models(pid, transport=httpx2.MockTransport(handler)) == {"models": ["m-a", "m-b"], "available": True}
    assert seen == [f"Bearer {KEY}"]
    profiles.close()


def test_bind_needs_a_key_and_the_price_is_optional(tmp_path, ring):
    from services.configs import ApiError
    from services.profiles import ProfileService

    profiles = ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    pid = profiles.create(
        "prof_b", profile_body(pricing=None, model_pricing={"m-small": {"input_usd_per_mtok": 1, "output_usd_per_mtok": 2}})
    )["id"]
    bound = profiles.bind(pid, "m-small", "test-family")
    assert (bound.input_usd_per_mtok, bound.output_usd_per_mtok, bound.family) == (1, 2, "test-family")
    unpriced = profiles.bind(pid, "m-other", None)
    assert unpriced.price()["source"] == "unknown" and unpriced.cost(1000, 1000) is None
    ring.data.clear()
    with pytest.raises(ApiError) as e:
        profiles.bind(pid, "m-small", None)
    assert e.value.code == "profile_key_missing"
    profiles.close()


def test_env_file_parser():
    path = Path(__file__).parent / "_env_parse.tmp"
    path.write_text("A=1\n # B=2\nexport C='x y'\nD = \"q\"\nbad line\n", encoding="utf-8")
    try:
        assert read_env_file(path) == {"A": "1", "C": "x y", "D": "q"}
    finally:
        path.unlink()


def llm_config(pid, **kw):
    from test_api import config

    body = config()
    llm = {"provider_profile_id": pid, "model": "m-small", "temperature": 0.2, "model_family": "fake"}
    body["agents"][0]["policy"] = {"kind": "llm", "llm": llm}
    body["seeds"] = [3]
    return {**body, **kw}


@pytest.fixture
def llm_client(tmp_path, ring, monkeypatch):
    from fakellm import cooperator

    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    fake = FakeLLM(cooperator(10.5))
    with TestClient(create_app(tmp_path, use_dbos=False, key_backend=ring, llm_transport=fake.transport)) as c:
        c.fake = fake
        yield c


def test_llm_run_is_refused_while_paid_calls_are_off(client):
    pid = client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    assert check(client.post("/api/runs", json=llm_config(pid)), "/runs", "post")["code"] == "paid_calls_disabled"


def test_llm_run_on_a_fake_endpoint_records_calls_without_the_key(llm_client, tmp_path):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    body = check(c.post("/api/runs", json=llm_config(pid)), "/runs", "post")
    rid = body["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    detail = check(c.get(f"/api/runs/{rid}"), "/runs/{run_id}")
    assert detail["status"] == "completed" and detail["calls"] > 0 and detail["cost_usd"] > 0 and detail["models"] == ["m-small"]
    events = c.get(f"/api/runs/{rid}/events", params={"limit": 5000}).json()["events"]
    header = events[0]["header"]
    a = next(x for x in header["agents"] if x["id"] == "A")
    assert a["provider_profile_id"] == pid and a["model"] == "m-small" and a["prompt_hash"].startswith("sha256:")
    calls = [e for e in events if e["type"] == "llm_call"]
    assert calls and all(e["agent_id"] == "A" and e["provider_profile_id"] == pid for e in calls)
    prices = c.get(f"/api/runs/{rid}/series").json()["sellers"]["A"]["price"]
    assert set(prices) == {10.5}
    obs = check(c.get(f"/api/runs/{rid}/observations", params={"agent_id": "A", "t": 2}), "/runs/{run_id}/observations")
    assert obs["calls"] and obs["calls"][0]["messages"][0]["role"] == "system"
    sent = next(b for b in c.fake.bodies if any(m.get("content") == obs["calls"][0]["messages"][1]["content"] for m in b["messages"]))
    shown = {t["name"]: sorted(t["parameters"]["properties"]) for t in obs["calls"][0]["tools"]}
    assert shown == {t["function"]["name"]: sorted(t["function"]["parameters"]["properties"]) for t in sent.get("tools", [])}
    assert all(t["tool_id"] == t["name"] and t["tool_version"] == "1" for t in obs["calls"][0]["tools"])
    assert all(KEY.encode() not in raw for raw in c.fake.raw)
    text = c.get(f"/api/runs/{rid}/episode.jsonl").text + json.dumps(events) + json.dumps(obs)
    no_key_anywhere(tmp_path, text)


def test_llm_run_stops_at_the_budget_cap(llm_client):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid, budget={"max_usd": 0.00005})).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    detail = c.get(f"/api/runs/{rid}").json()
    assert detail["status"] == "stopped" and detail["end_reason"] == "budget_exceeded" and detail["cost_usd"] <= 0.00005


def test_an_unpriced_llm_run_needs_a_cap_that_can_act_and_a_key(llm_client, ring, monkeypatch):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body(pricing=None)).json()["id"]
    err = check(c.post("/api/runs", json=llm_config(pid, budget={"max_usd": 1.0})), "/runs", "post")
    assert err["code"] == "budget_unenforceable" and err["errors"][0]["path"] == "budget/max_calls"
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    rid = check(c.post("/api/runs", json=llm_config(pid, budget={"max_usd": 1.0, "max_calls": 500})), "/runs", "post")["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    calls = c.get(f"/api/runs/{rid}/events", params={"types": "llm_call", "limit": 5000}).json()["events"]
    assert calls and all(e["cost_usd"] is None for e in calls)
    header = c.get(f"/api/runs/{rid}/events", params={"types": "run_started"}).json()["events"][0]["header"]
    price = next(a for a in header["agents"] if a["id"] == "A")["price"]
    assert price["source"] == "unknown" and price["input_usd_per_mtok"] is None
    ring.data.clear()
    assert check(c.post("/api/runs", json=llm_config(pid, budget={"max_calls": 500})), "/runs", "post")["code"] == "profile_key_missing"


def test_placebo_branches_of_an_llm_run_are_separate_samples(llm_client):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid)).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    kids = c.post(f"/api/runs/{rid}/branches", json={"at": 10, "rounds": 3, "n": 3, "intervention": {"type": "placebo"}}).json()["run_ids"]
    hashes = []
    for kid in kids:
        c.app.state.svc.wait(kid, timeout=60)
        calls = c.get(f"/api/runs/{kid}/events", params={"types": "llm_call", "limit": 5000}).json()["events"]
        index = {e["sample_index"] for e in calls}
        assert calls and len(index) == 1
        assert not any(e["cache_hit"] for e in calls), "every placebo child is a fresh draw, none replays the parent"
        hashes.append({e["request_hash"] for e in calls})
    indexes = {
        c.get(f"/api/runs/{kid}/events", params={"types": "llm_call", "limit": 1}).json()["events"][0]["sample_index"] for kid in kids
    }
    assert indexes == {1, 2, 3}
    assert not (hashes[0] & hashes[1]) and not (hashes[1] & hashes[2]) and not (hashes[0] & hashes[2])


def test_two_llm_sellers_in_a_symmetric_round_do_not_share_a_reply(llm_client):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    body = llm_config(pid)
    body["agents"][1]["policy"] = {"kind": "llm", "llm": dict(body["agents"][0]["policy"]["llm"])}
    body["agents"][1]["capability"] = body["agents"][0]["capability"]
    rid = c.post("/api/runs", json=body).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    calls = c.get(f"/api/runs/{rid}/events", params={"types": "llm_call", "limit": 5000}).json()["events"]
    first = [e for e in calls if e["t"] == 1]
    assert {e["agent_id"] for e in first} == {"A", "B"} and not any(e["cache_hit"] for e in first)


def test_branches_and_recovery_of_an_llm_run_obey_the_paid_calls_switch(llm_client):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid)).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    c.app.state.svc.llm.transport = None
    resp = c.post(f"/api/runs/{rid}/branches", json={"at": 10, "rounds": 3, "n": 1, "intervention": {"type": "placebo"}})
    assert (resp.status_code, resp.json()["code"]) == (409, "paid_calls_disabled")
    with pytest.raises(ApiError) as e:
        c.app.state.svc.builder(rid)
    assert e.value.code == "paid_calls_disabled"


def test_an_llm_run_with_a_rejected_key_ends_failed_not_completed(llm_client):
    c = llm_client
    c.fake.brain = lambda body: 401
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid)).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    assert check(c.get(f"/api/runs/{rid}"), "/runs/{run_id}")["status"] == "failed"
    events = check(c.get(f"/api/runs/{rid}/events", params={"types": "llm_call,run_ended"}), "/runs/{run_id}/events")["events"]
    assert [(e["type"], e["error"]) for e in events] == [("llm_call", "auth"), ("run_ended", "auth")], "the failed round is kept (D3)"
    assert events[-1]["status"] == "failed"
    assert len(c.fake.bodies) == 1


def test_top_p_and_per_agent_max_output_tokens_reach_the_request(llm_client):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    body = llm_config(pid)
    body["agents"][0]["policy"]["llm"] |= {"top_p": 0.9, "max_output_tokens": 123}
    rid = c.post("/api/runs", json=body).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    sent = c.fake.bodies[0]
    assert sent["top_p"] == 0.9 and (sent.get("max_tokens") or sent.get("max_completion_tokens")) == 123


def test_a_setting_that_does_nothing_is_rejected(llm_client):
    pid = llm_client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    body = llm_config(pid)
    body["agents"][0]["policy"]["llm"]["summary_prompt_id"] = "summary_v1"
    assert check(llm_client.post("/api/runs", json=body), "/runs", "post")["code"] == "validation_error"


def test_a_branch_budget_replaces_the_parents_budget_in_the_children(llm_client):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid)).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    kids = c.post(
        f"/api/runs/{rid}/branches",
        json={"at": 10, "rounds": 3, "n": 2, "intervention": {"type": "placebo"}, "budget": {"max_usd": 1.0, "max_calls": 2}},
    ).json()["run_ids"]
    for kid in kids:
        c.app.state.svc.wait(kid, timeout=60)
        paid = [
            e for e in c.get(f"/api/runs/{kid}/events", params={"types": "llm_call", "limit": 5000}).json()["events"] if not e["cache_hit"]
        ]
        assert len(paid) <= 2
        assert json.loads(c.app.state.db.run_config(kid)["config_json"])["budget"]["max_calls"] == 2


def test_the_cost_bound_is_the_cap_times_the_runs(llm_client):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    body = llm_config(pid, seeds=[1, 2, 3], budget={"max_usd": 0.25})
    started = check(c.post("/api/runs", json=body), "/runs", "post")
    assert started["estimate_usd_max"] == 0.75
    for rid in started["run_ids"]:
        c.app.state.svc.wait(rid, timeout=60)
    kids = check(
        c.post(
            f"/api/runs/{started['run_ids'][0]}/branches",
            json={"at": 5, "rounds": 2, "n": 2, "intervention": {"type": "placebo"}, "budget": {"max_usd": 0.1}},
        ),
        "/runs/{run_id}/branches",
        "post",
    )
    assert kids["estimate_usd_max"] == 0.2
    for kid in kids["run_ids"]:
        c.app.state.svc.wait(kid, timeout=60)


def test_profile_models_lists_ids_from_the_endpoint_and_returns_no_key(llm_client):
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    body = check(c.get(f"/api/provider-profiles/{pid}/models"), "/provider-profiles/{profile_id}/models")
    assert body == {"models": ["m-large", "m-small"], "available": True}
    assert c.fake.models_auth == f"Bearer {KEY}"
    assert check(c.get("/api/provider-profiles/prof_nope/models"), "/provider-profiles/{profile_id}/models")["code"] == "not_found"


@pytest.mark.parametrize(("key", "shown"), [("sk-abcdefghi", "…"), ("sk-abcdefghijklmnop", "…"), ("sk-abcdefghijklmnopq", "sk-…nopq")])
def test_the_hint_shows_seven_characters_only_for_keys_of_20_or_more(key, shown):
    assert hint(key) == shown


def test_a_prompt_file_edit_after_start_does_not_change_the_run(llm_client, monkeypatch):
    from services import llm_runs

    c = llm_client
    original = llm_runs.default_prompt()
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid)).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    stored = json.loads(c.app.state.db.run_config(rid)["config_json"])
    assert stored["agents"][0]["policy"]["llm"]["system_prompt"] == original
    monkeypatch.setattr(llm_runs, "default_prompt", lambda: "EDITED PROMPT")
    before = len(c.fake.bodies)
    body = {"at": 10, "rounds": 2, "n": 2, "intervention": {"type": "placebo"}}
    for kid in c.post(f"/api/runs/{rid}/branches", json=body).json()["run_ids"]:
        c.app.state.svc.wait(kid, timeout=60)
    systems = {b["messages"][0]["content"].split("\n\nMarket facts: ")[0] for b in c.fake.bodies[before:]}
    assert systems == {original}, "the stored prompt, then the market facts (DECISIONS-22 entry 5.1)"


def test_a_restart_with_paid_calls_off_keeps_an_llm_run_pending_until_a_restart_that_allows_them(tmp_path, ring, monkeypatch):
    import threading
    import time

    from adapters.store import RunsDB
    from fakellm import cooperator

    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    monkeypatch.setenv("SIM_DBOS_PATH", str(tmp_path / "data" / "dbos.sqlite"))
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    gate, brain = threading.Event(), cooperator(10.5)

    def slow(body):
        gate.wait(10)
        return brain(body)

    fake = FakeLLM(slow)
    db = RunsDB(tmp_path / "data" / "runs.sqlite")

    def events(rid):
        return [e for e in db.events(rid) if e.type in ("run_status_changed", "run_ended")]

    with TestClient(create_app(tmp_path, use_dbos=True, key_backend=ring, llm_transport=fake.transport)) as c:
        pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
        (rid,) = check(c.post("/api/runs", json=llm_config(pid)), "/runs", "post")["run_ids"]
        c.post(f"/api/runs/{rid}/pause")
        gate.set()
        deadline = time.time() + 30
        while not any(e.type == "run_status_changed" and e.status == "paused" for e in events(rid)):
            assert time.time() < deadline, "the run never paused"
            time.sleep(0.05)

    with TestClient(create_app(tmp_path, use_dbos=True, key_backend=ring)):
        deadline = time.time() + 30
        while not any(e.type == "run_status_changed" and e.status == "recovering" for e in events(rid)):
            assert time.time() < deadline, "the restart without paid calls did not mark the run recovering"
            time.sleep(0.05)
    held = events(rid)
    assert "run_ended" not in {e.type for e in held}
    assert [e.reason for e in held if e.status == "recovering"] == ["paid_calls_disabled"]

    with TestClient(create_app(tmp_path, use_dbos=True, key_backend=ring, llm_transport=fake.transport)) as c:
        assert c.app.state.svc.wait(rid, timeout=60)["status"] == "completed"
    assert [(e.status, e.end_reason) for e in events(rid) if e.type == "run_ended"] == [("completed", "horizon")]


def test_store_profiles_need_a_keyring_and_the_default_refuses_with_a_hint(tmp_path, monkeypatch):
    """Handoff simplify-cut B4: no keyring unless SIM_KEYRING=1; the sprint path is .env profiles."""
    monkeypatch.delenv("SIM_KEYRING", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    with TestClient(create_app(tmp_path, use_dbos=False)) as c:
        resp = c.post("/api/provider-profiles", json=profile_body())
        assert resp.status_code == 409 and resp.json()["code"] == "keyring_disabled"
        assert "SIM_PROFILE_" in resp.json()["message"]


def test_a_replicate_rerun_makes_new_calls_and_replicate_0_replays_the_cache(llm_client):
    """User, 25 Oct ("全部重跑", DECISIONS-36 §5): the same config again replays the cache; replicate 1 draws new replies."""
    c = llm_client
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]

    def calls_of(config):
        rid = c.post("/api/runs", json=config).json()["run_ids"][0]
        c.app.state.svc.wait(rid, timeout=60)
        return c.get(f"/api/runs/{rid}/events", params={"types": "llm_call", "limit": 5000}).json()["events"]

    first, again, rerun = calls_of(llm_config(pid)), calls_of(llm_config(pid)), calls_of(llm_config(pid, replicate=1))
    assert first and not any(e["cache_hit"] for e in first)
    assert all(e["cache_hit"] for e in again), "replicate 0 replays the first run"
    assert rerun and not any(e["cache_hit"] for e in rerun), "replicate 1 makes new calls"
    assert {e["sample_index"] for e in rerun} == {1_000_000}
    assert not ({e["request_hash"] for e in first} & {e["request_hash"] for e in rerun})
