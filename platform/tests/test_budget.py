# ruff: noqa: F401, F811 (the fixtures root and fake_client come from test_item2_api and test_d13)
"""DECISIONS-18 entries 2 and 3: no output limit (max_output_tokens null, not sent), the client timeout per model, the
unified thinking setting, a length finish as a model-check failure and the seconds per round. Fake endpoint, no paid
call."""

import json
import subprocess
import sys

import pytest

import services.llm_runs as llm_runs
from fakellm import FakeLLM, cooperator
from test_api import check
from test_d13 import C1_REQUEST, PILOT, check_dir, fake_client, judge, model_check, run_condition, talker, with_trail
from test_item2_api import OPENCODE, app, root, write_env
from test_profiles import llm_config as store_llm_config


def env_run(root, monkeypatch, **llm) -> tuple[FakeLLM, list[float], list[dict]]:
    """One fake run on the env profile opencode_chat (an OpenCode Go model name that Pydantic AI does not know) with these
    LLM settings: the wire bodies, the client timeouts and the trail lines."""
    write_env(root, **OPENCODE)
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")  # the fake transport answers; nothing leaves the machine
    timeouts = []
    real = llm_runs.Endpoint

    def endpoint(*a, **kw):
        timeouts.append(kw["timeout_s"])
        return real(*a, **kw)

    monkeypatch.setattr(llm_runs, "Endpoint", endpoint)
    fake = FakeLLM(cooperator(10.5))
    with app(root, fake=fake) as c:
        body = store_llm_config("opencode_chat", budget={"max_calls": 400})
        body["agents"][0]["policy"]["llm"].update(llm, model="minimax-m3.1-flash-free")
        rid = check(c.post("/api/runs", json=body), "/runs", "post")["run_ids"][0]
        c.app.state.svc.wait(rid, timeout=60)
        assert c.get(f"/api/runs/{rid}").json()["status"] == "completed"
        lines = [json.loads(ln) for ln in (root / "runs" / rid / "trail.jsonl").read_text(encoding="utf-8").splitlines()]
    return fake, timeouts, lines


@pytest.mark.parametrize(("thinking", "sent"), [("low", "low"), ("xhigh", "xhigh"), (True, "medium"), (None, None)])
def test_thinking_reaches_the_wire_as_the_providers_field_and_is_in_the_trail(root, monkeypatch, thinking, sent):
    fake, _, lines = env_run(root, monkeypatch, thinking=thinking)
    assert fake.bodies and {b.get("reasoning_effort") for b in fake.bodies} == {sent}
    assert "thinking" not in set().union(*fake.bodies), "Pydantic AI maps the setting; it is not sent as is"
    reasoning = {"reasoning_effort": sent} if sent else None
    assert lines and [ln["settings"].get("sent_reasoning") for ln in lines] == [reasoning] * len(lines), "as sent (D23 entry 4)"
    assert not any("thinking" in ln["settings"] for ln in lines)


def test_a_null_max_output_tokens_sends_no_limit(root, monkeypatch):
    fake, _, lines = env_run(root, monkeypatch, max_output_tokens=None)
    assert fake.bodies and not any({"max_tokens", "max_completion_tokens"} & set(b) for b in fake.bodies)
    assert lines and not any("max_tokens" in ln["settings"] for ln in lines)
    fake, _, _ = env_run(root, monkeypatch, max_output_tokens=8000)
    assert {b["max_completion_tokens"] for b in fake.bodies} == {8000}, "a set limit is sent"


def test_the_timeout_of_the_config_reaches_the_client(root, monkeypatch):
    assert env_run(root, monkeypatch, timeout_s=300)[1] == [300]
    assert env_run(root, monkeypatch)[1] == [60], "the default stays 60 s"


@pytest.mark.parametrize("bad", [{"timeout_s": 0}, {"timeout_s": 601}, {"thinking": "max"}, {"max_output_tokens": 0}])
def test_a_bad_budget_setting_is_refused(root, bad):
    write_env(root, **OPENCODE)
    with app(root) as c:
        body = store_llm_config("opencode_chat")
        body["agents"][0]["policy"]["llm"].update(bad)
        assert c.post("/api/runs", json=body).status_code == 422


def test_the_estimate_counts_a_call_with_no_limit_at_the_planning_figure(root):
    from adapters.llm.policy import OPEN_OUTPUT_TOKENS

    write_env(root, **OPENCODE)
    with app(root) as c:
        body = store_llm_config("opencode_chat")
        body["agents"][0]["policy"]["llm"]["max_output_tokens"] = 400
        capped = c.post("/api/runs/estimate", json=body)
        body["agents"][0]["policy"]["llm"]["max_output_tokens"] = None
        none = c.post("/api/runs/estimate", json=body)
    assert capped.status_code == none.status_code == 200
    assert none.json()["tokens_max"]["output"] > capped.json()["tokens_max"]["output"]
    assert any(str(OPEN_OUTPUT_TOKENS) in a for a in none.json()["assumptions"])


# model_check.py: one budget, seconds per round, length on any round


def llm_sellers(body):
    return [a["policy"]["llm"] for a in body["agents"] if a["policy"]["kind"] == "llm"]


def test_the_budget_sets_no_limit_300_s_and_thinking_low_and_the_check_has_12_calls():
    mc = model_check()
    want = {"max_output_tokens": None, "timeout_s": 300, "thinking": "low", "thinking_on_timeout": False}
    for as_check in (False, True):
        body = mc.budget(C1_REQUEST, as_check)
        assert llm_sellers(body) and all({k: s[k] for k in want} == want for s in llm_sellers(body))
        assert {k: v for k, v in body.items() if k not in ("agents", "name", "budget", "batch_budget")} == {
            k: v for k, v in C1_REQUEST.items() if k not in ("agents", "name", "budget", "batch_budget")
        }
    assert mc.budget(C1_REQUEST, True)["budget"]["max_calls"] == mc.budget(C1_REQUEST, True)["batch_budget"]["max_calls"] == 12
    assert mc.budget(C1_REQUEST)["budget"] == C1_REQUEST["budget"]
    assert llm_sellers(C1_REQUEST)[0].get("max_output_tokens") == 2000, "the base request is not changed"


def test_the_check_and_the_paid_stage_share_one_key_and_another_budget_has_another():
    mc = model_check()
    code = PILOT.parents[3]
    check, stage = mc.budget(C1_REQUEST, True), mc.budget(C1_REQUEST)
    assert mc.key(check, code, code) == mc.key(stage, code, code)
    assert mc.key(stage, code, code) != mc.key(C1_REQUEST, code, code)
    other = json.loads(json.dumps(stage))
    other["agents"][0]["policy"]["llm"]["thinking"] = "medium"
    assert mc.key(other, code, code) != mc.key(stage, code, code)
    retry = json.loads(json.dumps(stage))
    retry["agents"][0]["policy"]["llm"]["thinking_on_timeout"] = "minimal"
    assert mc.key(retry, code, code) != mc.key(stage, code, code), "thinking_on_timeout is in the key (CR-90 5)"
    assert mc.key(stage, code, code, "openai_responses") != mc.key(stage, code, code, "openai_chat"), "D23 entry 4"
    assert mc.key(stage, code, code) == mc.key(stage, code, code, None), "no protocol: the key of before"


def stamp(events, per_round: dict[int, float], rest: float = 1.0):
    """The events with wall times: the run starts at 0 s, round t settles after the seconds of rounds 1..t; a round
    missing from per_round takes `rest` seconds."""
    at, clock = {}, 0.0
    for t in sorted({int(e["t"]) for e in events if e["type"] == "settled"}):
        clock += per_round.get(t, rest)
        at[t] = clock

    def wall(s):
        return f"2026-10-22T{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}Z"

    out = []
    for e in events:
        if e["type"] == "run_started":
            e = {**e, "wall_time": wall(0)}
        elif e["type"] == "settled" and int(e["t"]) in at:
            e = {**e, "wall_time": wall(at[int(e["t"])])}
        out.append(e)
    return out


def check_run(fake_client):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    zip_bytes = fake_client.get(f"/api/runs/{events[0]['run_id']}/export", params={"format": "zip"}).content
    return events, zip_bytes


def no_limit(out):
    (out / "request.json").write_text(json.dumps(model_check().budget(C1_REQUEST, True)), encoding="utf-8")
    return out


def test_seconds_per_round_and_their_mean_are_in_the_report(fake_client, tmp_path):
    mc = model_check()
    events, zip_bytes = check_run(fake_client)
    r = judge(mc, with_trail(check_dir(tmp_path, stamp(events, {1: 30.0, 2: 50.0, 3: 7.0})), zip_bytes))
    assert r["pass"], r
    assert (r["seconds_per_round"][1], r["seconds_per_round"][2], r["mean_seconds_per_round"]) == (30.0, 50.0, 40.0)
    assert (r["median_seconds_per_round"], r["max_seconds_per_round"]) == (1.0, 50.0), "all settled rounds, 1 s each after round 3"


@pytest.mark.parametrize(
    ("per_round", "rest", "why"),
    [
        ({1: 300.0}, 120.0, []),
        ({}, 121.0, ["median round time 121 s above 120 s"]),
        ({3: 301.0}, 10.0, ["a round took 301 s, above 300 s"]),  # a round after round 2 counts too
        ({}, 301.0, ["median round time 301 s above 120 s", "a round took 301 s, above 300 s"]),
    ],
)
def test_the_latency_gate_fails_on_a_median_round_above_120_s_or_any_round_above_300_s(fake_client, tmp_path, per_round, rest, why):
    """DECISIONS-22 entry 5 §8."""
    events, zip_bytes = check_run(fake_client)
    r = judge(model_check(), with_trail(check_dir(tmp_path, stamp(events, per_round, rest)), zip_bytes))
    assert (r["pass"], r["reasons"]) == (not why, why)


def test_with_no_limit_the_check_passes_without_the_half_rule_and_a_length_finish_in_any_round_fails(fake_client, tmp_path):
    mc = model_check()
    events, zip_bytes = check_run(fake_client)
    big = [{**e, "output_tokens": 5000} if e["type"] == "llm_call" else e for e in events]
    assert judge(mc, with_trail(no_limit(check_dir(tmp_path, big, "open")), zip_bytes))["pass"], "no limit: no half rule"
    assert not judge(mc, with_trail(check_dir(tmp_path, big, "2000"), zip_bytes))["pass"], "2,000: the half rule holds"
    late = max((e for e in events if e["type"] == "llm_call"), key=lambda e: e["t"])
    assert late["t"] > 2
    r = judge(mc, with_trail(no_limit(check_dir(tmp_path, events, "late")), zip_bytes, {late["seq"]: {"finish_reason": "length"}}))
    assert not r["pass"] and any("cut" in x for x in r["reasons"]), r


def test_the_cli_writes_the_budget(tmp_path):
    def run(*a):
        return subprocess.run([sys.executable, str(PILOT / "model_check.py"), *map(str, a)], capture_output=True, text=True)

    assert run("budget", PILOT / "pilot-1c-c1.json", tmp_path / "check.json", "check").returncode == 0
    assert run("budget", PILOT / "pilot-1c-c1.json", tmp_path / "c1c.json").returncode == 0
    check, c1c = (json.loads((tmp_path / f).read_text(encoding="utf-8")) for f in ("check.json", "c1c.json"))
    assert check["budget"]["max_calls"] == 12 and c1c["budget"] == C1_REQUEST["budget"]
    assert llm_sellers(check) == llm_sellers(c1c)


@pytest.mark.parametrize("missing", [{"output_tokens": 0}, {"input_tokens": 0}, {"output_tokens": None}])
def test_a_returned_call_with_no_usage_fails_the_check_in_any_round(fake_client, tmp_path, missing):
    """CR-85 2: a stream that carries no usage gives 0 tokens; the check must fail, not read a mean of 0 as room."""
    mc = model_check()
    events, zip_bytes = check_run(fake_client)
    late = max((e for e in events if e["type"] == "llm_call" and not e.get("error")), key=lambda e: e["t"])
    blind = [{**e, **missing} if e["seq"] == late["seq"] else e for e in events]
    r = judge(mc, with_trail(no_limit(check_dir(tmp_path, blind)), zip_bytes))
    assert not r["pass"] and r["usage_missing"] == 1
    returned = sum(1 for e in events if e["type"] == "llm_call" and not e.get("error"))
    assert [x for x in r["reasons"] if "usage missing" in x] == [
        f"usage missing on 1 of {returned} returned call(s): input or output tokens 0 or null"
    ]
    assert judge(mc, with_trail(no_limit(check_dir(tmp_path, events, "whole")), zip_bytes))["usage_missing"] == 0
