""".env profiles and defaults (DECISIONS-9 §16, CR-13 P1, P2, m1), the run estimate, the 5-hour window and the FE-9b fixes.

Every endpoint is a fake transport; no paid call.
"""

import importlib.metadata
import io
import json
import logging
import re
import shutil
import traceback
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adapters.keys import TOKEN, KeyStore, read_env_file
from adapters.llm.endpoint import PROTOCOLS
from api.app import create_app
from fakellm import FakeLLM, cooperator
from services.profiles import ProfileService, StartupError, paid_calls_allowed
from test_api import ROOT, check, config
from test_profiles import KEY, MemoryKeyring, profile_body
from test_profiles import llm_config as store_llm_config
from condcopy import draft_conditions

ENV_KEY = "sk-env-ABCDEFGHIJ0123456789"
REPO = Path(__file__).parents[1]


def write_env(root: Path, **values: str) -> None:
    (root / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


OPENCODE = {
    "SIM_PROFILE_OPENCODE_CHAT_BASE_URL": "https://fake.local/inference/go/openai/v1",
    "SIM_PROFILE_OPENCODE_CHAT_PROTOCOL": "openai_chat",
    "SIM_PROFILE_OPENCODE_CHAT_KEY": ENV_KEY,
}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    return tmp_path


def app(root, ring=None, fake=None):
    return TestClient(create_app(root, use_dbos=False, key_backend=ring or MemoryKeyring(), llm_transport=fake.transport if fake else None))


def no_env_key(root: Path, *texts: str) -> None:
    for t in texts:
        assert ENV_KEY not in t
    for f in root.rglob("*"):
        if f.is_file() and f.name != ".env":
            assert ENV_KEY.encode() not in f.read_bytes(), f


# .env profiles


def test_an_env_profile_is_listed_read_only_with_only_a_hint_of_its_key(root):
    write_env(root, **OPENCODE, SIM_PROFILE_OPENCODE_CHAT_MAX_CALLS_PER_5H="100")
    with app(root) as c:
        rows = check(c.get("/api/provider-profiles"), "/provider-profiles")
        p = next(r for r in rows if r["id"] == "opencode_chat")
        assert (p["source"], p["read_only"], p["protocol"], p["key_set"], p["key_store"]) == ("env", True, "openai_chat", True, "env")
        assert p["key_env"] == "SIM_PROFILE_OPENCODE_CHAT_KEY" and p["max_calls_per_5h"] == 100 and p["pricing"] is None
        assert p["rate_window"] == {"max_calls_per_5h": 100, "used": 0, "room": 100, "rate_limited": 0}
        for resp in (
            c.patch("/api/provider-profiles/opencode_chat", json={"name": "x"}),
            c.patch("/api/provider-profiles/opencode_chat", json={"api_key": "sk-other-0123456789abcdef"}),
            c.delete("/api/provider-profiles/opencode_chat"),
        ):
            assert resp.status_code == 409 and resp.json()["code"] == "profile_read_only"
        # The test call stays allowed on a .env profile; only the paid gate refuses it.
        assert c.post("/api/provider-profiles/opencode_chat/test", json={"model": "m"}).json()["code"] == "paid_calls_disabled"
        no_env_key(root, json.dumps(rows))


def test_paid_calls_can_be_enabled_from_the_env_file(root, monkeypatch):
    """Handoff simplify-cut A2: SIM_ALLOW_PAID=1 in <root>/.env turns the gate on with nothing exported."""
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    write_env(root, **OPENCODE)
    with app(root) as c:
        assert check(c.get("/api/health"), "/health")["paid_calls_enabled"] is False
    write_env(root, **OPENCODE, SIM_ALLOW_PAID="1")
    with app(root) as c:
        assert check(c.get("/api/health"), "/health")["paid_calls_enabled"] is True
    assert paid_calls_allowed(root / ".env") is True
    monkeypatch.setenv("SIM_ALLOW_PAID", "0")  # the process environment wins over the file
    assert paid_calls_allowed(root / ".env") is False


def test_an_llm_run_on_an_env_profile_records_protocol_path_and_an_unknown_price(root, monkeypatch):
    write_env(root, **OPENCODE)
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    fake = FakeLLM(cooperator(10.5))
    with app(root, fake=fake) as c:
        body = store_llm_config("opencode_chat", budget={"max_calls": 400})
        rid = check(c.post("/api/runs", json=body), "/runs", "post")["run_ids"][0]
        c.app.state.svc.wait(rid, timeout=60)
        header = c.get(f"/api/runs/{rid}/events", params={"types": "run_started"}).json()["events"][0]["header"]
        a = next(x for x in header["agents"] if x["id"] == "A")
        assert (a["provider_profile_id"], a["protocol"], a["base_path"], a["price"]["source"]) == (
            "opencode_chat",
            "openai_chat",
            "/inference/go/openai/v1",
            "unknown",
        )
        assert fake.auth == f"Bearer {ENV_KEY}"
        events = c.get(f"/api/runs/{rid}/events", params={"limit": 5000}).text
        no_env_key(root, events)


def test_the_session_header_carries_one_stable_id_per_run_and_agent_and_stays_out_of_the_cache_key(root, monkeypatch):
    """PILOT-1: OpenCode Go refuses a call without x-opencode-session. Every call also names our client."""
    write_env(root, **OPENCODE, SIM_PROFILE_OPENCODE_CHAT_SESSION_HEADER="x-opencode-session")
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    fake = FakeLLM(cooperator(10.5))
    with app(root, fake=fake) as c:
        assert c.get("/api/provider-profiles/opencode_chat").json()["session_header"] == "x-opencode-session"
        body = store_llm_config("opencode_chat", budget={"max_calls": 400})
        first = check(c.post("/api/runs", json=body), "/runs", "post")["run_ids"][0]
        c.app.state.svc.wait(first, timeout=60)
        assert len(fake.headers) > 1
        assert {h["x-opencode-session"] for h in fake.headers} == {f"{first}:A"}
        assert {h["user-agent"] for h in fake.headers} == {f"collusion-sim/{importlib.metadata.version('collusion-sim')}"}
        sent = len(fake.headers)
        second = check(c.post("/api/runs", json=body), "/runs", "post")["run_ids"][0]  # same seed, another session id
        c.app.state.svc.wait(second, timeout=60)
        assert len(fake.headers) == sent  # every call a cache hit: the session is not in the key
        calls = c.get(f"/api/runs/{second}/events", params={"types": "llm_call", "limit": 5000}).json()["events"]
        assert calls and all(x["cache_hit"] for x in calls)


@pytest.mark.parametrize("name", ["Authorization", "x-api-key", "User-Agent", "x opencode"])
def test_a_session_header_that_is_a_key_header_or_not_a_name_stops_the_start(tmp_path, name):
    write_env(tmp_path, **OPENCODE, SIM_PROFILE_OPENCODE_CHAT_SESSION_HEADER=name)
    with pytest.raises(StartupError) as e:
        ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=MemoryKeyring()))
    assert "SIM_PROFILE_OPENCODE_CHAT_" in str(e.value) and ENV_KEY not in str(e.value)


def test_a_bad_env_variable_stops_the_start_and_names_the_variable_never_a_value(root, capsys):
    write_env(root, **{**OPENCODE, "SIM_PROFILE_OPENCODE_CHAT_PROTOCOL": "grpc"})
    log = io.StringIO()
    handler = logging.StreamHandler(log)
    logging.getLogger().addHandler(handler)
    try:
        with pytest.raises(StartupError) as e:
            with app(root):
                pass
    finally:
        logging.getLogger().removeHandler(handler)
    assert "SIM_PROFILE_OPENCODE_CHAT_PROTOCOL" in str(e.value)
    out = capsys.readouterr()
    no_env_key(root, str(e.value), repr(e.value), "".join(traceback.format_exception(e.value)), out.out, out.err, log.getvalue())


@pytest.mark.parametrize(
    "extra, needle",
    [
        ({"SIM_PROFILE_OPENCODE_CHAT_KYE": ENV_KEY}, "SIM_PROFILE_OPENCODE_CHAT_KYE"),
        ({"SIM_PROFILE_LONELY_KEY": ENV_KEY}, "SIM_PROFILE_LONELY_BASE_URL"),
        ({"SIM_PROFILE_PROF_01ABC_BASE_URL": "https://x.test/v1"}, "prof_"),
        ({"SIM_PROFILE_OPENCODE_CHAT_MAX_CALLS_PER_5H": "lots"}, "SIM_PROFILE_OPENCODE_CHAT_MAX_CALLS_PER_5H"),
        ({"SIM_PROFILE_OPENCODE_CHAT_BASE_URL": ENV_KEY}, "base_url"),
    ],
)
def test_env_profile_variables_are_parsed_one_way_or_the_start_stops(tmp_path, extra, needle):
    write_env(tmp_path, **{**OPENCODE, **extra})
    with pytest.raises(StartupError) as e:
        ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=MemoryKeyring()))
    assert needle in str(e.value) and ENV_KEY not in str(e.value)


def test_suffixes_are_matched_longest_first(tmp_path):
    write_env(tmp_path, SIM_PROFILE_A_MAX_BASE_URL="https://x.test/v1", SIM_PROFILE_A_MAX_MAX_CONCURRENCY="3", SIM_PROFILE_A_MAX_KEY="")
    profiles = ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=MemoryKeyring()))
    assert profiles.record("a_max").max_concurrency == 3


@pytest.mark.parametrize("where", ["env_file", "environment"])
def test_an_env_profile_without_its_key_variable_stops_the_start_and_never_takes_another_source(tmp_path, monkeypatch, where):
    """CR-19 m4: with no SIM_PROFILE_<ID>_KEY line, a keyring entry or SIM_KEY_<ID> must not become its key."""
    ring = MemoryKeyring()
    ring.set_password(KeyStore(tmp_path / ".env", backend=ring).service, "nokey", KEY)
    env = {"SIM_PROFILE_NOKEY_BASE_URL": "https://x.test/v1"}
    if where == "env_file":
        write_env(tmp_path, **env, SIM_KEY_NOKEY=ENV_KEY)
    else:
        write_env(tmp_path, **env)
        monkeypatch.setenv("SIM_KEY_NOKEY", ENV_KEY)
    with pytest.raises(StartupError) as e:
        ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    msg = str(e.value)
    assert "SIM_PROFILE_NOKEY_KEY" in msg and ENV_KEY not in msg and KEY not in msg


def test_an_empty_key_variable_means_no_key_not_another_source(tmp_path, monkeypatch):
    ring = MemoryKeyring()
    ring.set_password(KeyStore(tmp_path / ".env", backend=ring).service, "nokey", KEY)
    monkeypatch.setenv("SIM_KEY_NOKEY", ENV_KEY)
    write_env(tmp_path, SIM_PROFILE_NOKEY_BASE_URL="https://x.test/v1", SIM_PROFILE_NOKEY_KEY="")
    keys = KeyStore(tmp_path / ".env", backend=ring)
    ProfileService(tmp_path / "p.sqlite", keys)
    assert keys.get("nokey") is None


def test_one_id_in_the_store_and_in_env_stops_the_start_after_normalising(tmp_path):
    ring = MemoryKeyring()
    keys = KeyStore(tmp_path / ".env", backend=ring)
    ProfileService(tmp_path / "p.sqlite", keys).create("opencode-chat", profile_body())
    write_env(tmp_path, **OPENCODE)
    with pytest.raises(StartupError) as e:
        ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    assert "opencode-chat" in str(e.value) and ENV_KEY not in str(e.value) and KEY not in str(e.value)


@pytest.mark.parametrize("where", ["env_file", "environment"])
def test_a_store_profile_with_a_keyring_key_and_a_sim_key_variable_stops_the_start(tmp_path, monkeypatch, where):
    ring = MemoryKeyring()
    ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring)).create("prof_01ABC", profile_body())
    if where == "env_file":
        write_env(tmp_path, SIM_KEY_PROF_01ABC=ENV_KEY)
    else:
        monkeypatch.setenv("SIM_KEY_PROF_01ABC", ENV_KEY)
    with pytest.raises(StartupError) as e:
        ProfileService(tmp_path / "p.sqlite", KeyStore(tmp_path / ".env", backend=ring))
    msg = str(e.value)
    assert "prof_01ABC" in msg and "keyring" in msg and "SIM_KEY_PROF_01ABC" in msg
    assert ENV_KEY not in msg and KEY not in msg


def test_env_example_lists_the_variables_and_holds_no_key_like_value():
    path = REPO / ".env.example"
    values = read_env_file(path)
    commented = read_env_file_from_comments(path)
    plain = {*PROTOCOLS, "collusion-sim", "x-opencode-session"}  # protocol, keyring service and header names: long, not secret
    assert not [k for k, v in {**values, **commented}.items() if TOKEN.search(v) and not v.startswith("http") and v not in plain]
    assert not [k for k, v in values.items() if k.endswith("_KEY") and v]
    for name in ("SIM_PROFILE_OPENCODE_CHAT_BASE_URL", "SIM_PROFILE_OPENCODE_CHAT_PROTOCOL", "SIM_PROFILE_OPENCODE_CHAT_KEY"):
        assert name in values
    for name in ("SIM_ALLOW_PAID", "SIM_DEFAULT_SELLER_MODEL", "SIM_DEFAULT_JUDGE_MODEL", "SIM_PROFILE_OPENCODE_CHAT_MAX_CALLS_PER_5H"):
        assert name in values or name in commented


def read_env_file_from_comments(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


# Defaults


def test_defaults_come_from_the_allow_list_only(root, monkeypatch):
    write_env(
        root,
        SIM_DEFAULT_SELLER_PROFILE="opencode_chat",
        SIM_DEFAULT_SELLER_MODEL="m-small",
        SIM_DEFAULT_SELLER_B_MODEL="m-large",
        SIM_DEFAULT_JUDGE_MODEL="jev-1.13",
        SIM_DEFAULT_WIZARD_MODEL="not-served",
    )
    monkeypatch.setenv("SIM_DEFAULT_SELLER_SECRET", ENV_KEY)
    with app(root) as c:
        d = check(c.get("/api/defaults"), "/defaults")
    assert d["seller"] == {"profile_id": "opencode_chat", "model": "m-small", "agents": {"B": {"profile_id": None, "model": "m-large"}}}
    assert d["judge"]["model"] == "jev-1.13" and set(d) == {"seller", "judge", "judge2", "host_llm"}
    assert "not-served" not in json.dumps(d) and ENV_KEY not in json.dumps(d)


# Run estimate


def test_the_run_estimate_of_scripted_sellers_is_zero_calls(root):
    with app(root) as c:
        est = check(c.post("/api/runs/estimate", json=config(seeds=(1, 2))), "/runs/estimate", "post")
    assert est["n_runs"] == 2 and est["calls_max"] == 0 and est["total_usd_max"] == 0 and est["within_budget"] is True


def test_the_run_estimate_counts_every_step_and_attempt_and_follows_the_limits(root):
    ring = MemoryKeyring()
    with app(root, ring=ring) as c:
        pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
        body = store_llm_config(pid)
        est = check(c.post("/api/runs/estimate", json=body), "/runs/estimate", "post")
        rounds = est["per_run"]["max_rounds"]
        # A is an LLM in talk_and_act with a post grant: one talk and one act turn per round, 5 steps x 3 attempts each.
        assert est["per_run"]["calls"] == {"talk": rounds * 15, "act": rounds * 15, "one_call": 0, "summary": 0, "total_max": rounds * 30}
        assert est["total_usd_max"] > 0 and est["pricing_missing_for"] == [] and est["warnings"] == []
        two = check(c.post("/api/runs/estimate", json={**body, "limits": {"max_steps_per_turn": 2}}), "/runs/estimate", "post")
        assert two["calls_max"] == rounds * 2 * 2 * 3
        assert c.app.state.db.runs() == [], "an estimate stores nothing"


def test_the_run_estimate_names_missing_prices_and_a_too_small_window(root):
    write_env(root, **OPENCODE, SIM_PROFILE_OPENCODE_CHAT_MAX_CALLS_PER_5H="10")
    with app(root) as c:
        est = check(c.post("/api/runs/estimate", json=store_llm_config("opencode_chat")), "/runs/estimate", "post")
    assert est["total_usd_max"] is None and est["pricing_missing_for"] == ["opencode_chat"] and est["within_budget"] is None
    (w,) = est["rate_window"]
    assert (w["profile_id"], w["room"], w["enough"]) == ("opencode_chat", 10, False)
    assert any("5-hour window" in x for x in est["warnings"]) and any("budget_unenforceable" in x for x in est["warnings"])


# 5-hour window, end to end


def test_the_window_counts_calls_of_every_run_and_survives_a_restart(root, monkeypatch):
    write_env(root, **OPENCODE, SIM_PROFILE_OPENCODE_CHAT_MAX_CALLS_PER_5H="5")
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    body = store_llm_config("opencode_chat", budget={"max_calls": 400})
    with app(root, fake=FakeLLM(cooperator(10.5))) as c:
        first = c.post("/api/runs", json=body).json()["run_ids"][0]
        c.app.state.svc.wait(first, timeout=60)
        assert c.get(f"/api/runs/{first}").json()["end_reason"] == "rate_window"
        assert c.get("/api/provider-profiles/opencode_chat").json()["rate_window"]["room"] == 0
    with app(root, fake=FakeLLM(cooperator(10.5))) as c:
        second = c.post("/api/runs", json={**body, "seeds": [4]}).json()["run_ids"][0]  # another seed: no cache hit
        c.app.state.svc.wait(second, timeout=60)
        calls = c.get(f"/api/runs/{second}/events", params={"types": "llm_call"}).json()["events"]
        assert calls == [] and c.get(f"/api/runs/{second}").json()["end_reason"] == "rate_window"


def test_the_window_status_counts_calls_not_yet_stored_and_a_new_limit_keeps_one_window(root):
    """CR-19 m5."""
    write_env(root, **OPENCODE, SIM_PROFILE_OPENCODE_CHAT_MAX_CALLS_PER_5H="5")
    with app(root) as c:
        llm = c.app.state.svc.llm
        prof = llm.profiles.bind("opencode_chat", "m", None)
        window = llm.window(prof)
        window.take()
        window.take()
        assert c.get("/api/provider-profiles/opencode_chat").json()["rate_window"] == {
            "max_calls_per_5h": 5,
            "used": 2,
            "room": 3,
            "rate_limited": 0,
        }
        assert llm.window(prof.model_copy(update={"max_calls_per_5h": 3})) is window and window.room() == 1


def test_a_call_cap_of_the_batch_lets_an_unpriced_run_have_only_max_usd_but_a_plain_branch_needs_its_own(root, monkeypatch):
    """CR-19 m3: the shared batch cap can act, so the run needs no cap of its own; a branch does not share it."""
    write_env(root, **OPENCODE)
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    body = store_llm_config("opencode_chat", budget={"max_usd": 1.0})
    with app(root, fake=FakeLLM(cooperator(10.5))) as c:
        assert c.post("/api/runs", json=body).json()["code"] == "budget_unenforceable"
        started = check(c.post("/api/runs", json={**body, "batch_budget": {"max_calls": 400}}), "/runs", "post")
        rid = started["run_ids"][0]
        c.app.state.svc.wait(rid, timeout=60)
        err = check(
            c.post(f"/api/runs/{rid}/branches", json={"at": 15, "rounds": 2, "intervention": {"type": "placebo"}}),
            "/runs/{run_id}/branches",
            "post",
        )
        assert err["code"] == "budget_unenforceable"


# FE-9b backend issues 2-4


def test_schema_titles_are_labels_not_pydantic_defaults(root):
    with app(root) as c:
        defs = c.get("/api/schema/run-config").json()["$defs"]
    assert defs["BudgetSettings"]["properties"]["max_usd"]["title"] == "Max USD"
    assert defs["LlmSettings"]["properties"]["provider_profile_id"]["title"] == "Provider profile"
    assert defs["MemorySettings"]["properties"]["full_rounds"]["description"]


def test_price_schedule_prices_is_an_object_from_round_to_price(root):
    with app(root) as c:
        policies = check(c.get("/api/policies"), "/policies")
        schema = c.get("/api/schema/run-config").json()["$defs"]["PolicySettings"]["x-params-by-kind"]["price_schedule"]
    prices = next(p for p in next(x for x in policies if x["kind"] == "price_schedule")["params"] if p["name"] == "prices")
    assert prices["type"] == "object" and prices["schema"]["additionalProperties"] == {"type": "number"}
    assert schema["properties"]["prices"]["type"] == "object"


@pytest.mark.parametrize(
    "change, path",
    [
        ({"condition_id": "C1"}, "condition_id"),
        ({"call_mode": None}, "call_mode"),
        ({"budget": {}}, "budget/max_usd"),
    ],
)
def test_a_model_validator_error_names_its_field(root, change, path):
    with app(root) as c:
        err = check(c.post("/api/runs", json={**config(), **change}), "/runs", "post")
    assert err["code"] == "validation_error" and [e["path"] for e in err["errors"]] == [path]


@pytest.mark.parametrize("route", ["/runs", "/runs/estimate"])
def test_an_llm_policy_without_llm_settings_is_a_422_on_every_route(root, route):
    """Found in the live check: the estimate (and a start with paid calls on) read policy.llm before any check and gave 500."""
    body = config()
    body["agents"][0]["policy"] = {"kind": "llm", "params": {"provider_profile_id": "opencode_chat", "model": "m"}}
    with app(root) as c:
        err = check(c.post(f"/api{route}", json=body), route, "post")
    assert err["code"] == "validation_error" and [e["path"] for e in err["errors"]] == ["agents/0/policy/llm"]


# Q1: a multi-seed POST /runs with one batch budget


def test_a_batch_budget_caps_the_calls_of_all_seeds_of_one_request(root, monkeypatch):
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    ring = MemoryKeyring()
    with app(root, ring=ring, fake=FakeLLM(cooperator(10.5))) as c:
        pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
        body = {**store_llm_config(pid, budget={"max_calls": 400}), "seeds": [3, 4, 5], "batch_budget": {"max_calls": 30}}
        started = check(c.post("/api/runs", json=body), "/runs", "post")
        for rid in started["run_ids"]:
            c.app.state.svc.wait(rid, timeout=60)
        db = c.app.state.db
        assert sorted(db.batch_runs(started["batch_id"])) == sorted(started["run_ids"])
        assert 0 < db.paid_calls(started["run_ids"])["calls"] <= 30
        assert "budget_exceeded" in {c.get(f"/api/runs/{r}").json()["end_reason"] for r in started["run_ids"]}
        longest = max(started["run_ids"], key=lambda r: len(db.snapshots(r)))
        kids = c.post(f"/api/runs/{longest}/branches", json={"at": 2, "rounds": 1, "intervention": {"type": "placebo"}})
        assert kids.status_code == 202, kids.text
        child = RunConfigOf(c, kids.json()["run_ids"][0])
        assert child.batch_budget is None, "a branch batch does not inherit the parent's shared cap"


def RunConfigOf(c, run_id):
    from services.configs import RunConfig

    return RunConfig.model_validate_json(c.app.state.db.run_config(run_id)["config_json"])


# CR-24: the estimate runs the agent checks of a start


@pytest.mark.parametrize("route", ["/runs", "/runs/estimate"])
@pytest.mark.parametrize(
    "case",
    ["empty_profile", "empty_model", "unknown_profile", "no_key", "tit_for_tat_without_start"],
)
def test_the_estimate_refuses_every_agent_that_a_start_refuses_with_the_same_error(root, route, case):
    fake = FakeLLM(cooperator(10.5))
    ring = MemoryKeyring()
    with app(root, ring=ring, fake=fake) as c:
        pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
        body = store_llm_config(pid)
        llm = body["agents"][0]["policy"]["llm"]
        if case == "empty_profile":
            llm["provider_profile_id"] = ""
        elif case == "empty_model":
            llm["model"] = ""
        elif case == "unknown_profile":
            llm["provider_profile_id"] = "prof_unknown"
        elif case == "no_key":
            ring.data.clear()  # the key was removed from the keyring outside the app
        else:
            seller = next(a for a in body["agents"] if a["policy"]["kind"] != "llm")
            seller["policy"] = {"kind": "tit_for_tat", "params": {}}
        r = c.post(f"/api{route}", json=body)
        expected = {
            "empty_profile": (422, "validation_error"),
            "empty_model": (422, "validation_error"),
            "unknown_profile": (404, "not_found"),
            "no_key": (422, "profile_key_missing"),
            "tit_for_tat_without_start": (422, "validation_error"),
        }[case]
        assert (r.status_code, r.json()["code"]) == expected, r.text
        assert c.app.state.db.runs() == [] and not hasattr(fake, "auth"), "no call reached the endpoint"


def test_the_experiment_estimate_refuses_a_scripted_seller_with_bad_params_as_the_start_does(root):
    from test_experiments import body as experiment

    b = experiment()
    b["agents"] = [dict(a) for a in b["agents"]]
    b["agents"][1] = {"id": "B", "role": "seller", "policy": {"kind": "tit_for_tat", "params": {}}}
    with app(root) as c:
        for route in ("/api/experiments/estimate", "/api/experiments"):
            r = c.post(route, json=b)
            assert (r.status_code, r.json()["code"]) == (422, "validation_error"), (route, r.text)
        assert c.app.state.db.experiments() == []
