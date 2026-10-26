"""scripts/fake_llm_server.py: the app that the web check runs against (FE-realbackend-report.md). No paid call.

If the app, the prompts or the run config change so that the script no longer runs a seller, this test fails, so the web check does
not rot unseen."""

import importlib.util
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from test_api import ROOT

SCRIPT = ROOT / "scripts" / "fake_llm_server.py"


@pytest.fixture(scope="module")
def fake():
    spec = importlib.util.spec_from_file_location("fake_llm_server", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def exploratory_body(profile_id: str, scenario: dict) -> dict:
    llm = {"kind": "llm", "llm": {"provider_profile_id": profile_id, "model": "m-small", "model_family": "fake"}}
    grants = [
        {"tool_id": t, "params": p}
        for t, p in (
            ("set_price", {}),
            ("hold", {}),
            ("post", {"channels": ["public", "private_A_B"], "max_per_round": 1}),
            ("read_channel", {"channels": ["public", "private_A_B"]}),
        )
    ]
    return {
        "kind": "exploratory",
        "scenario_id": scenario["id"],
        "scenario_hash": scenario["hash"],
        "call_mode": "talk_and_act",
        "channels": {"public": True, "private_pairs": True, "news": True},
        "transparency": "rival_last_round",
        "split": "exploratory",
        "seeds": [7],
        "agents": [
            {"id": i, "role": "seller", "policy": llm, "capability": {"grants": grants, "max_queries_per_round": 3}} for i in ("A", "B")
        ],
        "clock": {"mode": "sync"},
        "budget": {"max_calls": 2000},
    }


def test_the_channel_comes_from_the_prompt(fake):
    assert fake.post_channel("Channels you can post to: public, private_A_B.\nYou may post") == "private_A_B"
    assert fake.post_channel("Channels you can post to: public.") == "public"
    assert fake.post_channel("no list") == "public"


def test_a_temporary_root_is_removed_when_the_server_stops_and_a_given_root_is_kept(fake, tmp_path):
    with fake.fake_root(None) as made:
        assert (made / "conditions").is_dir() and (made / "scenarios").is_dir()
    assert not made.exists()
    kept = fake.temp_root(None)
    try:
        with fake.fake_root(kept) as given:
            assert given == kept
        assert kept.is_dir()
    finally:
        import shutil

        shutil.rmtree(kept, ignore_errors=True)


def test_the_paid_switch_is_set_only_while_the_server_runs(fake, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    with fake.paid_switch():
        assert os.environ["SIM_ALLOW_PAID"] == "1"
    assert "SIM_ALLOW_PAID" not in os.environ
    monkeypatch.setenv("SIM_ALLOW_PAID", "0")
    with fake.paid_switch():
        assert os.environ["SIM_ALLOW_PAID"] == "1"
    assert os.environ["SIM_ALLOW_PAID"] == "0"


def test_the_delay_makes_each_reply_take_that_long(fake, monkeypatch):
    body = {"messages": [{"role": "user", "content": "x"}], "tools": []}
    monkeypatch.setattr(fake, "DELAY_S", 0.2)
    started = time.time()
    fake.brain(body)
    assert time.time() - started >= 0.2
    monkeypatch.setattr(fake, "DELAY_S", 0.0)
    started = time.time()
    fake.brain(body)
    assert time.time() - started < 0.1


def test_a_root_without_conditions_is_refused(fake, tmp_path):
    with pytest.raises(SystemExit):
        fake.temp_root(tmp_path)


def test_two_llm_sellers_post_set_prices_and_leave_a_trail_with_thinking(fake, tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)  # the fake transport needs no paid switch
    with fake.fake_root(None) as root, TestClient(fake.build_app(root)) as c:
        pid = c.post("/api/provider-profiles", json=fake.PROFILE).json()["id"]
        scenario = next(x for x in c.get("/api/scenarios").json() if x["id"] == "scn_mock_demo")
        started = c.post("/api/runs", json=exploratory_body(pid, scenario))
        assert started.status_code == 202, started.text
        (rid,) = started.json()["run_ids"]
        deadline = time.time() + 60
        while c.get(f"/api/runs/{rid}").json()["status"] in ("queued", "running") and time.time() < deadline:
            time.sleep(0.2)
        assert c.get(f"/api/runs/{rid}").json()["status"] == "completed"
        events = c.get(f"/api/runs/{rid}/events", params={"limit": 5000}).json()["events"]
        posts = [e for e in events if e["type"] == "posted"]
        assert posts and {e["channel_id"] for e in posts} == {"private_A_B"} and {e["text"] for e in posts} == {fake.OFFER}
        prices = [e["price"] for e in events if e["type"] == "acted" and e.get("price") is not None]
        assert prices[:2] == [6.0, 6.0] and 10.5 in prices, (
            "round 1 has no offer in the prompt; with a read_channel grant, later rounds show it"
        )
        trail = c.get(f"/api/runs/{rid}/calls/{next(e['seq'] for e in events if e['type'] == 'llm_call')}/trail").json()
        assert trail["thinking"] == fake.THINKING and trail["thinking_parts"] == 1
    assert json.dumps(fake.PROFILE).count("sk-FAKE") == 1, "the key in the script is a dummy"
