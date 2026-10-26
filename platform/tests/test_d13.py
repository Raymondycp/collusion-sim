"""DECISIONS-13 §1: fixed seller calls per round and a complete observation, on a fake endpoint (no paid call)."""

import json
import shutil
import sqlite3
from collections import Counter

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from adapters.llm.policy import NUDGE
from api.app import create_app
from fakellm import FakeLLM, call, cooperator, text, tools_offered
from services.research import ConditionFile
from simcore.callplan import push_gaps
from simkit import JOINT, capability, of
from simschema.events import Capability, ToolGrant
from test_api import ROOT
from test_llm_policy import llm_run
from test_profiles import MemoryKeyring, profile_body
from test_research import research_config, started
from condcopy import draft_conditions

CONDITIONS = ("C1", "C2a", "C2b", "C3-accept", "C3-silent")


@pytest.fixture
def fake_client(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    fake = FakeLLM(cooperator(10.5))
    with TestClient(create_app(tmp_path, use_dbos=False, key_backend=MemoryKeyring(), llm_transport=fake.transport)) as c:
        c.fake = fake
        yield c


def run_condition(c, cid: str) -> tuple[list[dict], dict]:
    """One pilot run of seed 1 with two LLM sellers on the fake endpoint: its events and the condition."""
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    llm = {"kind": "llm", "llm": {"provider_profile_id": pid, "model": "m-small", "model_family": "fake", "temperature": 0.2}}
    body = research_config(cid, seeds=(1,))
    for a in body["agents"]:
        a["policy"] = llm
    if cid == "C2b":  # C2b: a different model family per seller
        body["agents"][1]["policy"] = {"kind": "llm", "llm": {**llm["llm"], "model": "m-large", "model_family": "fake2"}}
    (rid,) = started(c, body)
    assert c.get(f"/api/runs/{rid}").json()["status"] == "completed", cid
    return c.get(f"/api/runs/{rid}/events", params={"limit": 5000}).json()["events"], c.get(f"/api/conditions/{cid}").json()


def calls_per_round(events: list[dict]) -> dict[str, set[int]]:
    """For each seller, the set of call counts over its rounds (one value when the count is fixed)."""
    n = Counter((e["agent_id"], e["t"]) for e in events if e["type"] == "llm_call")
    return {s: {k for (a, _), k in n.items() if a == s} for s in sorted({a for a, _ in n})}


@pytest.mark.parametrize("cid", CONDITIONS)
def test_each_condition_makes_one_act_call_and_one_talk_call_per_round_with_a_post_grant(fake_client, cid):
    events, cond = run_condition(fake_client, cid)
    calls = [e for e in events if e["type"] == "llm_call"]
    assert calls and all(e["error"] is None and e["attempt"] == 1 for e in calls)
    assert not [e for e in events if e["type"] == "tool_called"], "no read tool is ever called"
    talkers = {s for s, cap in cond["capabilities"].items() if any(g["tool_id"] == "post" for g in cap["grants"])}
    rounds = {e["t"] for e in events if e["type"] == "settled"}
    assert calls_per_round(events) == {s: {2 if s in talkers else 1} for s in ("A", "B")}
    for s in ("A", "B"):
        per = Counter(e["purpose"] for e in calls if e["agent_id"] == s)
        assert per == ({"act": len(rounds), "talk": len(rounds)} if s in talkers else {"act": len(rounds)})
    bodies = fake_client.fake.bodies
    assert all(tools_offered(b) == ["set_price", "hold"] for b in bodies if "set_price" in tools_offered(b))
    assert all(tools_offered(b) == [] for b in bodies if "set_price" not in tools_offered(b)), "a talk call offers no tool"


@pytest.mark.parametrize("cid", CONDITIONS)
def test_the_observation_holds_all_that_a_seller_needs(fake_client, cid):
    events, cond = run_condition(fake_client, cid)
    news = {(e["t"], a): e["text"] for e in events if e["type"] == "news_published" for a in e["visible"]}
    observed = [e for e in events if e["type"] == "observed"]
    assert observed
    for e in observed:
        o, s, t = e["observation"], e["agent_id"], e["t"]
        assert o["news"] is not None and o["recent_messages"] is not None, (cid, s, t)
        seen = [(n, text) for (n, a), text in news.items() if a == s and n <= t]
        assert [(n["t"], n["text"]) for n in o["news"]] == sorted(seen), (cid, s, t, "every news item it may see (D15 §1)")
        if (t, s) in news:
            assert news[(t, s)] in o["headlines"], (cid, s, t, "news it may see")
        if t == 1:
            continue
        own = o["own"]
        assert None not in (own["last_price"], own["last_quantity"], own["last_profit"]), (cid, s, t)
        history = o["own_history"]
        assert [r["t"] for r in history] == list(range(1, int(t))), (cid, s, t, "every round so far (D15 §1)")
        last = history[-1]
        assert (last["price"], last["quantity"], last["profit"]) == (own["last_price"], own["last_quantity"], own["last_profit"])
        assert set(o["rival_last_prices"]) == set(o["rival_history"]) == {"A", "B"} - {s}, (cid, s, t)
        for r, h in o["rival_history"].items():
            assert [p["t"] for p in h] == list(range(1, int(t))) and h[-1]["price"] == o["rival_last_prices"][r], (cid, s, t, r)
        assert o["new_messages"] is not None
        if cond["c3_isolation"] and e["purpose"] == "act":
            assert "channels" in o["masked"] and o["new_messages"] == o["recent_messages"] == [], "no message in the C3 act call"
    user = next(b["messages"][1]["content"] for b in fake_client.fake.bodies if "Round 2." in b["messages"][1]["content"])
    assert "The market runs" not in user and "look" not in user.lower()
    user = next(b["messages"][1]["content"] for b in fake_client.fake.bodies if "Round 9." in b["messages"][1]["content"])
    lines = user.splitlines()
    rounds = [f"Round {r}" for r in range(1, 8)] + ["Last round"]
    assert [ln.split(":")[0] for ln in lines[1:9]] == rounds, "every round 1-8, oldest first (DECISIONS-15 §1)"


def test_a_shuffle_mask_permutes_only_the_other_senders_messages():
    from simcore.state import Mask, Message, RunState
    from simcore.view import View
    from simschema.events import ChannelInfo

    caps = {s: capability(["private_A_B"]) for s in "AB"}
    view = View([ChannelInfo(id="private_A_B", kind="private", participants=["A", "B"])], caps, False)
    sent = [(t, s, f"{s} says {t}") for t in range(1, 7) for s in "AB"]
    state = RunState(
        messages=[Message(id=i, t=t, channel_id="private_A_B", sender=s, text=x, deliver_t=t + 1) for i, (t, s, x) in enumerate(sent)],
        masks=[Mask(channel_id="private_A_B", mode="shuffle", from_t=1)],
    )
    for reader, other in (("A", "B"), ("B", "A")):
        seen = view.messages(reader, "private_A_B", 7, state)
        assert [m["text"] for m in seen if m["from"] == reader] == [x for _, s, x in sent if s == reader], "own stay as sent"
        theirs = [m["text"] for m in seen if m["from"] == other]
        assert sorted(theirs) == sorted(x for _, s, x in sent if s == other), "the other's texts only, permuted among its slots"
        assert theirs != [x for _, s, x in sent if s == other], "the other's texts lose their timing"
        thread = view.observe(reader, 7, state, "talk").recent_messages
        assert [m.text for m in thread if m.sender == reader] == [x for _, s, x in sent if s == reader], "all, from round 1"


@pytest.mark.parametrize("pool", [0, 1, 2, 3])
def test_a_shuffle_is_a_derangement_of_2_or_more_messages_and_a_smaller_pool_is_a_recorded_noop(pool):
    """CR-60 n1 (lead): no other-sender message keeps its text when 2 or more are visible; with fewer the mask changes
    nothing and the observation says so. Each own count gives another seed (the visible count)."""
    from simcore.state import Mask, Message, RunState
    from simcore.view import View
    from simschema.events import ChannelInfo

    caps = {s: capability(["private_A_B"]) for s in "AB"}
    view = View([ChannelInfo(id="private_A_B", kind="private", participants=["A", "B"])], caps, False)
    theirs = [f"B says {i}" for i in range(pool)]
    for own in range(18):  # 17 + 3 = MAX_MESSAGES, so all are visible
        sent = [("B", x) for x in theirs] + [("A", f"A says {i}") for i in range(own)]
        msgs = [Message(id=i, t=1, channel_id="private_A_B", sender=s, text=x, deliver_t=2) for i, (s, x) in enumerate(sent)]
        masked = RunState(messages=msgs, masks=[Mask(channel_id="private_A_B", mode="shuffle", from_t=1)])
        seen = [m["text"] for m in view.messages("A", "private_A_B", 2, masked) if m["from"] == "B"]
        assert sorted(seen) == sorted(theirs)
        if pool >= 2:
            assert all(got != was for got, was in zip(seen, theirs)), (own, seen)
        else:
            assert seen == theirs
        assert view.observe("A", 2, masked, "talk").mask_noop is (pool < 2), own
        assert view.observe("A", 2, RunState(messages=msgs), "talk").mask_noop is False, "no mask, nothing to record"
        assert view.mask_noop("A", 2, masked, "act") is (pool < 2), "the act call reads the channel too (no c3_isolation)"
    shuffle = [Mask(channel_id="private_A_B", mode="shuffle", from_t=1)]
    for texts, noop in ((["Hold 10.5."] * 5, True), (["Hold 10.5."] * 3 + ["Go to 9."] * 2, False)):
        msgs = [Message(id=i, t=1, channel_id="private_A_B", sender="B", text=x, deliver_t=2) for i, x in enumerate(texts)]
        state = RunState(messages=msgs, masks=shuffle)
        seen = [m["text"] for m in view.messages("A", "private_A_B", 2, state)]
        assert view.observe("A", 2, state, "talk").mask_noop is noop, ("CR-60 n4: distinct texts", texts)
        assert (seen == texts) is noop, "equal texts read the same after the shuffle; two texts do not"
    isolated = View([ChannelInfo(id="private_A_B", kind="private", participants=["A", "B"])], caps, True)
    assert isolated.observe("A", 2, masked, "act").mask_noop is False, "an act call that reads no channel has no mask to record"


OFFER = "Offer: we both hold 10.5 from now on."


def talker(body):
    """Posts OFFER in every talk call, sets the joint price in every act call."""
    return call("set_price", price=JOINT, plan="Keep the price.") if "set_price" in tools_offered(body) else text(OFFER)


def user_text(body) -> str:
    return "\n".join(m["content"] or "" for m in body["messages"])


@pytest.mark.parametrize("cid", ("C3-accept", "C3-silent"))
def test_no_c3_act_request_holds_message_text(fake_client, cid):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, cid)
    assert [e for e in events if e["type"] == "posted" and e["text"] == OFFER], "the offer is posted"
    acts = [b for b in fake_client.fake.bodies if "set_price" in tools_offered(b)]
    talks = [b for b in fake_client.fake.bodies if "set_price" not in tools_offered(b)]
    assert acts and not [b for b in acts if OFFER in user_text(b) or "Message" in user_text(b)], "act calls, notes included"
    if cid == "C3-accept":
        assert any("Message in" in user_text(b) and OFFER in user_text(b) for b in talks), "the talk call shows it"
        assert any("Earlier message (round 1)" in user_text(b) and "from you" in user_text(b) for b in talks), "own, older"


def test_c3_silent_b_has_no_talk_turn_and_never_receives_the_offer(fake_client):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C3-silent")
    assert not [e for e in events if e["type"] == "observed" and e["agent_id"] == "B" and e["purpose"] == "talk"]
    posted = [e for e in events if e["type"] == "posted"]
    assert posted and all(e["agent_id"] == "A" and e["receivers"] == [] for e in posted), "not delivered"


def test_c2a_news_stays_visible_after_its_round(fake_client):
    events, _ = run_condition(fake_client, "C2a")
    published = [e for e in events if e["type"] == "news_published"]
    assert published, "C2a publishes news"
    first = published[0]
    s = first["visible"][0]
    later = [e for e in events if e["type"] == "observed" and e["agent_id"] == s and e["t"] == first["t"] + 1]
    assert later and all({"t": first["t"], "text": first["text"]} in e["observation"]["news"] for e in later)
    assert all(first["text"] not in e["observation"]["headlines"] for e in later), "headlines: this round only"
    rendered = f"Earlier news (round {int(first['t'])}): {first['text']}"
    assert any(rendered in user_text(b) for b in fake_client.fake.bodies)


def test_a_read_in_an_act_reply_with_no_queries_is_an_invalid_tool_and_takes_no_step(tmp_path):
    asked = []

    def brain(body):
        if not asked:
            asked.append(1)
            return call("get_news")
        return call("set_price", price=JOINT)

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(queries=0))
    calls = of(sink, "llm_call", agent_id="A")
    assert [(c.attempt, c.error) for c in calls] == [(1, "invalid_tool"), (2, None)]
    assert not of(sink, "tool_called"), "the read is not run and not recorded as a refused read"
    assert fake.bodies[1]["messages"] == fake.bodies[0]["messages"], "a retry, not a follow-up step"
    acted = of(sink, "acted", agent_id="A")[0]
    assert (acted.price, acted.fallback, acted.limits_hit) == (JOINT, False, [])


def test_a_condition_with_no_queries_refuses_a_seller_that_cannot_see_its_sales_news_or_channel():
    c1 = yaml.safe_load((ROOT / "conditions" / "C1.yaml").read_text(encoding="utf-8"))
    ConditionFile.model_validate(c1)
    for tool in ("get_own_sales", "get_news", "read_channel"):
        broken = {**c1, "sellers": {**c1["sellers"], "A": {**c1["sellers"]["A"]}}}
        broken["sellers"]["A"]["grants"] = [g for g in c1["sellers"]["A"]["grants"] if g["tool_id"] != tool]
        with pytest.raises(ValidationError, match="seller A has no queries and cannot see"):
            ConditionFile.model_validate(broken)
        with_queries = {**broken, "limits": {**c1["limits"], "max_queries_per_round": 1}}
        with pytest.raises(ValidationError, match="seller A has no queries and cannot see"):
            ConditionFile.model_validate(with_queries)  # push mode offers no read of the record (DECISIONS-15 §3.2)
        ConditionFile.model_validate({**with_queries, "observation_mode": "both"})


def test_push_gaps_names_each_missing_part():
    grants = [ToolGrant(tool_id=t) for t in ("get_own_sales", "get_news", "read_channel", "post")]
    assert push_gaps(Capability(grants=grants), news=True, channels=True) == []
    assert push_gaps(Capability(grants=[ToolGrant(tool_id="post")]), news=True, channels=True) == [
        "own price, sales and profit",
        "news",
        "channel messages",
    ]
    assert push_gaps(Capability(grants=[ToolGrant(tool_id="get_own_sales")]), news=False, channels=False) == []


# DECISIONS-13 §2: the model check (docs/sim/review/pilot-1/model_check.py), on a fake C1 run

PILOT = ROOT / "docs" / "sim" / "review" / "pilot-1"
C1_REQUEST = json.loads((PILOT / "pilot-1c-c1.json").read_text(encoding="utf-8"))


def model_check():
    import importlib.util

    spec = importlib.util.spec_from_file_location("model_check", PILOT / "model_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_dir(tmp_path, events, name="check"):
    out = tmp_path / name
    out.mkdir(parents=True, exist_ok=True)
    rid = events[0]["run_id"]
    (out / "request.json").write_text(json.dumps(C1_REQUEST), encoding="utf-8")
    exp = {"id": "exp_check", "cells": [{"parent": {"run_id": rid}}], "spend": {"calls": 6}}
    (out / "experiment.json").write_text(json.dumps(exp), encoding="utf-8")
    (out / f"events-{rid}.json").write_text(json.dumps({"events": events}), encoding="utf-8")
    return out


def judge(mc, out, code=ROOT, conditions=ROOT):
    return mc.judge(out, code, conditions, "abc")


def broken(events, kind, change, t=1):
    """A copy of the events with the first round-t event of `kind` changed."""
    out, done = [], False
    for e in events:
        if not done and e["type"] == kind and e["t"] == t:
            e, done = {**e, **change}, True
        out.append(e)
    assert done
    return out


def fetched_types() -> set[str]:
    line = next(ln for ln in (PILOT / "run-1c.sh").read_text(encoding="utf-8").splitlines() if ln.startswith("TYPES="))
    assert '--types "$TYPES"' in (PILOT / "run-1c.sh").read_text(encoding="utf-8")
    return set(line.removeprefix("TYPES=").split(","))


def types_read(path) -> set[str]:
    """Event types a pilot script reads: by("x", ...) calls and e["type"] == "x" tests."""
    import ast

    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "by" and node.args:
            arg = node.args[0]
        elif isinstance(node, ast.Compare) and isinstance(node.left, ast.Subscript) and getattr(node.left.slice, "value", None) == "type":
            arg = node.comparators[0]
        else:
            continue
        if isinstance(arg, ast.Constant):
            found.add(arg.value)
    return found


def test_run_1c_fetches_every_event_type_the_model_check_and_the_gates_read():
    read = {name: types_read(PILOT / name) for name in ("model_check.py", "gates.py")}
    assert {"observed", "talk_replied", "llm_call"} <= read["model_check.py"] and {"tool_called", "run_ended"} <= read["gates.py"]
    assert read["model_check.py"] | read["gates.py"] <= fetched_types()


def test_the_model_check_passes_on_the_fetched_types_only(fake_client, tmp_path):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    keep = fetched_types()
    got = judge(model_check(), check_dir(tmp_path, [e for e in events if e["type"] in keep]))
    assert got["pass"], got["reasons"]


def test_the_model_check_passes_two_clean_rounds_and_fails_each_named_defect(fake_client, tmp_path):
    mc = model_check()
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    good = judge(mc, check_dir(tmp_path, events))
    assert good["pass"] and good["reasons"] == [] and good["calls_rounds_1_2"] == 8 and good["max_output_tokens_seen"] == 20
    assert json.loads((tmp_path / "check" / "model-check.json").read_text(encoding="utf-8"))["pass"]
    defects = {
        "talk": broken(events, "talk_replied", {"outcome": "empty"}),
        "talk 2": broken(events, "talk_replied", {"outcome": "empty"}, t=2),
        "act": broken(events, "acted", {"fallback": True}),
        "act 2": broken(events, "acted", {"fallback": True}, t=2),
        "provider": broken(events, "llm_call", {"error": "provider_error", "provider_code": "UnexpectedModelBehavior"}),
        "tokens": [{**e, "output_tokens": 1500} if e["type"] == "llm_call" else e for e in events],
        "cut": broken(events, "llm_call", {"output_tokens": 2000}, t=2),
        "unsettled": [e for e in events if e["type"] != "settled"],
        "budget stop in round 2": [e for e in events if e["t"] < 2],
    }
    got = {name: judge(mc, check_dir(tmp_path / name, bad)) for name, bad in defects.items()}
    assert [name for name, r in got.items() if r["pass"] or not r["reasons"]] == []
    assert "round 2 did not settle" in got["budget stop in round 2"]["reasons"]
    assert any("provider error" in r for r in got["provider"]["reasons"])
    cut = got["cut"]
    assert cut["mean_output_tokens"] < 1000 and cut["max_output_tokens_seen"] == 2000, "the mean alone would pass"
    assert [r for r in cut["reasons"] if "reached max_output_tokens" in r] == cut["reasons"]
    assert cut["finish_reasons"] == {"none": 8}, "no export zip: no finish reason, the token test decides"


def with_trail(out, zip_bytes: bytes, change: dict[int, dict] | None = None):
    """Put the run's export zip in the check folder, with the trail lines of the seqs in `change` changed."""
    import io
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(zip_bytes))
    lines = [json.loads(x) for x in src.read("trail.jsonl").decode("utf-8").splitlines()]
    lines = [{**ln, **(change or {}).get(ln["seq"], {})} for ln in lines]
    rid = json.loads((out / "experiment.json").read_text(encoding="utf-8"))["cells"][0]["parent"]["run_id"]
    with zipfile.ZipFile(out / f"export-{rid}.zip", "w") as z:
        z.writestr("trail.jsonl", "".join(json.dumps(ln) + "\n" for ln in lines))
    return out


def test_the_model_check_uses_the_finish_reason_and_the_token_test_only_without_one(fake_client, tmp_path):
    assert 'runs export "$RUN" -o "$OUT/export-$RUN.zip"' in (PILOT / "run-1c.sh").read_text(encoding="utf-8")
    mc = model_check()
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    zip_bytes = fake_client.get(f"/api/runs/{rid}/export", params={"format": "zip"}).content
    first = next(e for e in events if e["type"] == "llm_call" and e["t"] == 1)
    good = judge(mc, with_trail(check_dir(tmp_path, events), zip_bytes))
    assert good["pass"] and good["finish_reasons"] == {"stop": 4, "tool_call": 4}, good
    length = judge(mc, with_trail(check_dir(tmp_path, events, "length"), zip_bytes, {first["seq"]: {"finish_reason": "length"}}))
    assert not length["pass"] and length["max_output_tokens_seen"] == 20, "cut by the provider's reason, at 20 tokens"
    assert [r for r in length["reasons"] if "call(s) cut" in r] == length["reasons"]
    big = broken(events, "llm_call", {"output_tokens": 2000}, t=2)
    assert judge(mc, with_trail(check_dir(tmp_path, big, "stop"), zip_bytes))["pass"], "a reason other than length: no token test"
    null = judge(mc, with_trail(check_dir(tmp_path, big, "null"), zip_bytes, {e["seq"]: {"finish_reason": None} for e in events}))
    assert not null["pass"] and null["finish_reasons"] == {"none": 8}, "no reason: the token test decides"


def test_the_model_check_reports_per_model_whether_thinking_came_back(fake_client, tmp_path):
    mc = model_check()
    reports = {}
    for name, brain in (("plain", talker), ("thinking", lambda b: {**talker(b), "reasoning_content": "Hold 10.5."})):
        fake_client.fake.brain = brain
        with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:  # else the second run hits the first run's rows
            conn.execute("DELETE FROM calls")
        events, _ = run_condition(fake_client, "C1")
        zip_bytes = fake_client.get(f"/api/runs/{events[0]['run_id']}/export", params={"format": "zip"}).content
        reports[name] = judge(mc, with_trail(check_dir(tmp_path, events, name), zip_bytes))
    replies = sum(e["type"] == "llm_call" for e in events)
    assert reports["plain"]["pass"] and reports["thinking"]["pass"], "a report, not a pass rule"
    assert reports["plain"]["thinking"] == {"m-small": {"replies": replies, "with_parts": 0, "with_text": 0, "unknown": 0}}
    assert reports["thinking"]["thinking"] == {"m-small": {"replies": replies, "with_parts": replies, "with_text": replies, "unknown": 0}}
    assert judge(mc, check_dir(tmp_path, events, "no zip"))["thinking"] == {}


def test_the_model_check_judges_every_parent_run(fake_client, tmp_path):
    mc = model_check()
    fake_client.fake.brain = talker
    good = run_condition(fake_client, "C1")[0]
    other = run_condition(fake_client, "C1")[0]
    out = check_dir(tmp_path, good)
    rids = [good[0]["run_id"], other[0]["run_id"]]
    (out / f"events-{rids[1]}.json").write_text(json.dumps({"events": broken(other, "acted", {"fallback": True}, t=2)}), encoding="utf-8")
    (out / "experiment.json").write_text(
        json.dumps({"id": "exp_check", "cells": [{"parent": {"run_id": r}} for r in rids]}), encoding="utf-8"
    )
    got = judge(mc, out)
    assert not got["pass"] and got["run_ids"] == rids and got["calls_rounds_1_2"] == 16
    assert got["reasons"] == [f"{rids[1]}: seller A: no valid set_price or hold in round 2"], got["reasons"]


def test_the_model_check_fails_when_round_2_has_no_message_to_answer(fake_client, tmp_path):
    events, _ = run_condition(fake_client, "C1")  # cooperator: PASS in every talk call
    got = judge(model_check(), check_dir(tmp_path, events))
    assert got["reasons"] == ["round 2 has no message to answer (no seller posted in round 1)"]


def test_the_model_check_fails_without_the_whole_record_or_with_a_resent_reply(fake_client, tmp_path):
    import io
    import zipfile

    mc = model_check()
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    zip_bytes = fake_client.get(f"/api/runs/{events[0]['run_id']}/export", params={"format": "zip"}).content
    assert judge(mc, with_trail(check_dir(tmp_path, events), zip_bytes))["pass"]
    for name, history in (("old server", None), ("empty", {})):  # CR-80 2: {} is not the record either
        bad = [{**e, "observation": {**e["observation"], "rival_history": history}} if e["type"] == "observed" else e for e in events]
        got = judge(mc, check_dir(tmp_path, bad, name))
        assert len(got["reasons"]) == 4 and all("does not hold round 1" in r for r in got["reasons"]), (name, "2 sellers x talk, act")
    lines = [json.loads(x) for x in zipfile.ZipFile(io.BytesIO(zip_bytes)).read("trail.jsonl").decode("utf-8").splitlines()]
    line = next(ln for ln in lines if ln["t"] == 2 and ln["purpose"] == "act")
    earlier = next(ln for ln in lines if ln["t"] == 1 and ln["agent_id"] == line["agent_id"])
    resent = earlier["messages"] + line["messages"]  # an earlier request and reply sent again as chat history
    got = judge(mc, with_trail(check_dir(tmp_path, events, "resent"), zip_bytes, {line["seq"]: {"messages": resent}}))
    assert got["reasons"] == [
        f"1 first attempt(s) not one system prompt, one user prompt and the reply (seller, round, purpose): "
        f"[('{line['agent_id']}', {line['t']!r}, '{line['purpose']}')]"
    ], got["reasons"]
    # CR-80 1: a nudge retry (attempt 2: the text reply, then NUDGE as a second user prompt) is legal
    request, reply = line["messages"]
    text_reply = {**reply, "parts": [{"part_kind": "text", "content": "I hold."}]}
    nudge = {**request, "parts": [{**request["parts"][-1], "content": NUDGE}]}
    retry = {"attempt": 2, "messages": [request, text_reply, nudge, reply]}
    assert judge(mc, with_trail(check_dir(tmp_path, events, "nudge"), zip_bytes, {line["seq"]: retry}))["pass"]


def copy_tree(tmp_path):
    """The files that shape a request, copied, so a test can change one."""
    root = tmp_path / "tree"
    for rel in model_check().CODE_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, root / rel)
    shutil.copytree(ROOT / "conditions", root / "conditions")  # an exact copy: the key covers the files as they are
    return root


def test_a_paid_stage_needs_a_passing_check_with_the_same_settings_and_request_files(fake_client, tmp_path):
    mc = model_check()
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    tree, runs = copy_tree(tmp_path), tmp_path / "1c-run"
    k = mc.key(C1_REQUEST, tree, tree)
    assert k == mc.key(C1_REQUEST, ROOT, ROOT), "a copy of the same files has the same key"
    request = check_dir(runs, events, f"check-{k}") / "request.json"
    assert "no model check for key" in mc.verify(runs, request, tree, tree)
    assert mc.judge(runs / f"check-{k}", tree, tree, "commit-1")["pass"]
    assert mc.verify(runs, request, tree, tree) is None
    rest = json.loads((PILOT / "pilot-1c-rest.json").read_text(encoding="utf-8"))
    (tmp_path / "rest.json").write_text(json.dumps(rest), encoding="utf-8")
    assert mc.verify(runs, tmp_path / "rest.json", tree, tree) is None, "rest has the same sellers and sampling as C1"
    rest["agents"][0]["policy"]["llm"]["max_output_tokens"] = 400
    (tmp_path / "rest.json").write_text(json.dumps(rest), encoding="utf-8")
    assert "no model check for key" in mc.verify(runs, tmp_path / "rest.json", tree, tree), "other sampling settings"
    assert {"src/adapters/llm/endpoint.py", "src/simcore/tools.py"} <= set(mc.CODE_FILES), "CR-60 n2"
    for rel in (*mc.CODE_FILES, "conditions/C1.yaml", "conditions/prompts.draft.yaml"):
        changed = copy_tree(tmp_path / rel.replace("/", "_"))
        (changed / rel).write_bytes((changed / rel).read_bytes() + b"\n# changed\n")
        assert mc.key(C1_REQUEST, changed, changed) != k, rel
        assert "no model check for key" in mc.verify(runs, request, changed, changed), rel
    crlf = copy_tree(tmp_path / "crlf")
    (crlf / "src/simcore/view.py").write_bytes((crlf / "src/simcore/view.py").read_bytes().replace(b"\n", b"\r\n"))
    assert mc.key(C1_REQUEST, crlf, crlf) == k, "line ends do not change the key"


def passed_cli(check) -> int:
    """`model_check.py passed CHECK`, as run-1c.sh calls it: exit 0 when CHECK holds a pass."""
    import subprocess
    import sys

    return subprocess.run([sys.executable, "-I", str(PILOT / "model_check.py"), "passed", str(check)]).returncode


def test_run_1c_refuses_a_second_check_after_a_pass():
    script = (PILOT / "run-1c.sh").read_text(encoding="utf-8")
    first = script.index('OUT="$RUN_DIR/check-$KEY"')
    refuse = script.index('! py "$HERE/model_check.py" passed "$OUT" || {', first)
    retry = script.index('[ ! -e "$OUT/experiment.json" ] || OUT="$OUT-2"', first)
    assert first < refuse < retry, "the pass test reads check-<key>/ before the retry folder is chosen"


def test_a_failed_check_may_have_one_retry_folder(fake_client, tmp_path):
    mc = model_check()
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    runs = tmp_path / "1c-run"
    k = mc.key(C1_REQUEST, ROOT, ROOT)
    first = check_dir(runs, broken(events, "acted", {"fallback": True}), f"check-{k}")
    assert not judge(mc, first)["pass"]
    assert "failed" in mc.verify(runs, first / "request.json", ROOT, ROOT)
    assert judge(mc, check_dir(runs, events, f"check-{k}-2"))["pass"]
    assert mc.verify(runs, first / "request.json", ROOT, ROOT) is None
    assert [passed_cli(runs / f"check-{k}{r}") for r in ("", "-2", "-9")] == [1, 0, 1], "exit 0 only for a passing folder"
    third = check_dir(tmp_path / "other", events, f"check-{k}-3")
    judge(mc, third)
    assert "no model check" in mc.verify(tmp_path / "other", first / "request.json", ROOT, ROOT), "only one retry counts"
