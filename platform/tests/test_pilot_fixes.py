"""PILOT-1b defects D1-D4 and the PILOT-1c fixes (docs/sim/review/PILOT-1-report.md), on a fake endpoint: no paid call."""

import json
import shutil
from collections import Counter

import httpx2
import pytest
from fastapi.testclient import TestClient

from adapters.llm import Endpoint, EndpointError, Request
from adapters.llm.policy import NUDGE
from adapters.store import StoreSink
from api.app import create_app
from fakellm import NO_CHOICES, call, cooperator, last_is_tool_result, text, tools_offered
from fakewire import streamed
from judge.views import render
from simcore.recorder import Recorder, _hash_line
from simcore.tools import TOOLS
from simcore.view import QueryBudget, ToolBox
from simkit import JOINT, capability, of
from simschema.events import SCHEMA_VERSION, SIM_EVENT, EpisodeHeader
from test_api import ROOT, check
from test_llm_policy import KEY, PROFILE, llm_run
from test_profiles import llm_client, llm_config, profile_body, ring  # noqa: F401
from condcopy import draft_conditions

PRIVATE = "private_A_B"
TWO = ["public", PRIVATE]  # two postable channels: the talk call keeps the post tool
SAY = "Your reply is posted verbatim to channel private_A_B. Reply PASS to stay silent."
NO_LOOKUP = "no look-up is available in this call"


def talking(body) -> bool:
    return "set_price" not in tools_offered(body)


def tool_results(body) -> list[dict]:
    return [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]


def test_d1_a_post_to_an_unknown_channel_is_a_tool_error_and_the_model_can_post_again(tmp_path):
    def brain(body):
        if not talking(body):
            return call("set_price", price=JOINT)
        done = tool_results(body)
        if not done:
            return call("post", channel_id="private", text="Shall we both hold 10.5?")
        return call("post", channel_id=PRIVATE, text="Shall we both hold 10.5?") if len(done) == 1 else text("Done.")

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=TWO), private_pairs=True, public=True)
    refusal = tool_results([b for b in fake.bodies if talking(b)][1])[0]
    assert refusal["refused"] is True and PRIVATE in refusal["reason"]
    assert [(p.channel_id, p.text) for p in of(sink, "posted", agent_id="A")] == [(PRIVATE, "Shall we both hold 10.5?")]
    assert of(sink, "run_ended")[0].status == "completed"


def test_d2_with_two_channels_the_talk_tools_list_the_channel_ids_and_the_talk_prompt_names_them(tmp_path):
    def brain(body):
        return text("Nothing to say.") if talking(body) else call("set_price", price=JOINT)

    _, fake, *_ = llm_run(brain, tmp_path, rounds=2, cap=capability(channels=TWO), private_pairs=True, public=True)
    talk = next(b for b in fake.bodies if talking(b) and b["messages"][-1]["content"].startswith("Round 2."))
    act = next(b for b in fake.bodies if not talking(b))
    schema = {
        t["function"]["name"]: sorted(t["function"]["parameters"]["properties"]["channel_id"]["enum"])
        for b in (talk, act)
        for t in b["tools"]
        if t["function"]["name"] in ("post", "read_channel")
    }
    assert tools_offered(talk) == ["post"], "a talk call offers no read tool"
    assert schema == {"post": sorted(TWO), "read_channel": sorted(TWO)}
    assert "Channels you can post to: " in talk["messages"][-1]["content"]
    assert "Channels you can post to" not in act["messages"][-1]["content"]


def test_with_one_channel_the_talk_call_has_no_post_tool_and_says_where_the_reply_goes(tmp_path):
    def brain(body):
        return text("PASS") if talking(body) else call("set_price", price=JOINT)

    _, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=[PRIVATE]), private_pairs=True)
    talk = next(b for b in fake.bodies if talking(b))
    assert tools_offered(talk) == [], "no post tool and no read tool"
    assert talk["messages"][-1]["content"].endswith(SAY) and talk.get("tool_choice") != "required"
    assert SAY not in next(b for b in fake.bodies if not talking(b))["messages"][-1]["content"]


def test_a_text_talk_reply_is_the_post_and_every_talk_turn_is_recorded(tmp_path):
    replies = iter([" Shall we both hold 10.5?\n", "PASS", "pass.", ""])

    def brain(body):
        return text(next(replies)) if talking(body) else call("set_price", price=JOINT)

    cap = capability(channels=[PRIVATE], see_rivals=False, queries=0)
    sink, *_ = llm_run(brain, tmp_path, rounds=4, cap=cap, private_pairs=True)
    replied = of(sink, "talk_replied", agent_id="A")
    assert [(r.t, r.outcome, r.channel_id) for r in replied] == [
        (1, "posted", PRIVATE),
        (2, "pass", PRIVATE),
        (3, "pass", PRIVATE),
        (4, "empty", PRIVATE),
    ]
    assert [r.text for r in replied] == ["Shall we both hold 10.5?", None, None, None], "tier A: posted text only"
    assert all(r.tier == "A" for r in replied)
    unposted = of(sink, "talk_text", agent_id="A")
    assert [(u.t, u.text, u.tier) for u in unposted] == [(2, "PASS", "B"), (3, "pass.", "B")], "CR-46 M1: unposted text is tier B"
    assert [(p.t, p.channel_id, p.text) for p in of(sink, "posted")] == [(1, PRIVATE, "Shall we both hold 10.5?")]
    assert [a.messages for a in of(sink, "acted", agent_id="A")] == [1, 0, 0, 0]


def test_a_talk_turn_with_no_usable_reply_is_recorded_as_an_error(tmp_path):
    def brain(body):
        return call("nope") if talking(body) else call("set_price", price=JOINT)

    sink, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=[PRIVATE]), private_pairs=True)
    assert [(r.outcome, r.text) for r in of(sink, "talk_replied")] == [("error", None)] and not of(sink, "posted")


def test_d3_a_failed_runs_calls_stay_in_its_record_and_its_spend(llm_client, monkeypatch):  # noqa: F811
    c = llm_client
    commits, write = [], StoreSink.write
    monkeypatch.setattr(
        StoreSink, "write", lambda self, events, snaps: commits.append([e.type for e in events]) or write(self, events, snaps)
    )
    replies = iter([text("PASS")])
    c.fake.brain = lambda body: next(replies, 401)  # one good reply, then the key is rejected mid-round
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid)).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    run = check(c.get(f"/api/runs/{rid}"), "/runs/{run_id}")
    assert (run["status"], run["calls"]) == ("failed", 2) and len(c.fake.bodies) == 2
    assert c.app.state.svc.db.paid_calls([rid])["calls"] == 2, "experiment and batch spend read the same events"
    events = check(c.get(f"/api/runs/{rid}/events", params={"limit": 5000}), "/runs/{run_id}/events")["events"]
    assert [e["seq"] for e in events] == list(range(len(events))) and events[-1]["type"] == "run_ended"
    assert [e["error"] for e in events if e["type"] == "llm_call"] == [None, "auth"]
    assert commits[-1].count("llm_call") == 2 and commits[-1][-1] == "run_ended", "CR-38 m2: the failed round and run_ended are one commit"
    assert events[-1]["t"] == max(e["t"] for e in events) == 1, "run_ended has the t of the failed round"


def test_d4_an_act_turn_forces_a_tool_call_and_a_text_reply_gets_one_nudged_retry(tmp_path):
    def brain(body):
        return call("set_price", price=JOINT) if body["messages"][-1]["content"] == NUDGE else text("I will charge 10.5.")

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(see_rivals=False))
    assert [b["tool_choice"] for b in fake.bodies] == ["required", "required"]
    assert fake.bodies[1]["messages"][-2:] == [{"role": "assistant", "content": "I will charge 10.5."}, {"role": "user", "content": NUDGE}]
    calls = of(sink, "llm_call", agent_id="A")
    assert [(c.attempt, c.error) for c in calls] == [(1, "invalid_tool"), (2, None)], "the nudged retry is a counted call"
    acted = of(sink, "acted", agent_id="A")[0]
    assert (acted.price, acted.fallback) == (JOINT, False)


def test_post_is_not_offered_when_no_channel_can_take_a_post(tmp_path):
    _, fake, *_ = llm_run(cooperator(), tmp_path, rounds=1, cap=capability(post=[PRIVATE]))  # granted, but the run has no channel
    assert fake.bodies and not any("post" in tools_offered(b) for b in fake.bodies)


def test_posts_to_a_wrong_channel_stop_at_max_steps_and_the_round_goes_on(tmp_path):
    def brain(body):
        return call("post", channel_id="general", text="Hello there.") if talking(body) else call("set_price", price=JOINT)

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=TWO), private_pairs=True, public=True)
    assert sum(talking(b) for b in fake.bodies) == 5 and not of(sink, "posted")
    assert [r.outcome for r in of(sink, "talk_replied")] == ["max_steps"]
    acted = of(sink, "acted", agent_id="A")[0]
    assert (acted.price, acted.limits_hit) == (JOINT, ["max_steps_per_turn"])


def test_a_text_reply_in_a_one_call_turn_gets_the_nudge(tmp_path):
    def brain(body):
        return call("set_price", price=JOINT) if body["messages"][-1]["content"] == NUDGE else text("Let us see.")

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=["public"]), call_mode="one_call", public=True)
    assert [b["tool_choice"] for b in fake.bodies] == ["required", "required"] and fake.bodies[1]["messages"][-1]["content"] == NUDGE
    assert not of(sink, "acted", agent_id="A")[0].fallback


READS = ("get_news", "get_own_sales", "get_rival_prices", "read_channel")


def always_reads(body):
    """A seller that reads whenever a read tool is offered, one read per call, then talks or prices."""
    args = {"read_channel": {"channel_id": PRIVATE}, "get_rival_prices": {"rounds": 1}, "get_own_sales": {"rounds": 1}}
    offered = [t for t in tools_offered(body) if t in READS]
    if offered:
        return call(offered[0], **args.get(offered[0], {}))
    return text("Hello there.") if talking(body) else call("set_price", price=JOINT)


def calls_per_seller_round(sink) -> int:
    """The most LLM calls one seller made in one round."""
    return max(Counter((e.agent_id, e.t) for e in of(sink, "llm_call")).values())


def test_an_always_reading_seller_uses_at_most_three_calls_per_round_with_one_query(tmp_path):
    cap = capability(channels=[PRIVATE], queries=1)
    sink, fake, *_ = llm_run(always_reads, tmp_path, rounds=3, cap=cap, private_pairs=True)
    assert not any(set(tools_offered(b)) & set(READS) for b in fake.bodies if talking(b)), "a talk call offers no read tool"
    assert [len(of(sink, "tool_called", agent_id="A", t=t)) for t in (1, 2, 3)] == [1, 1, 1]
    assert not [e for e in of(sink, "tool_called", agent_id="A") if not e.allowed], "no read is refused"
    after = [b for b in fake.bodies if not talking(b) and b["messages"][-1]["role"] == "tool"]
    assert after and all(tools_offered(b) == ["set_price", "hold"] for b in after)
    acted = of(sink, "acted", agent_id="A")
    assert all(not a.fallback and a.limits_hit == [] for a in acted) and of(sink, "posted", agent_id="A")
    assert calls_per_seller_round(sink) == 3, "1 talk + 1 read + 1 set_price"


def test_the_observation_holds_the_rivals_last_price_and_the_new_messages(tmp_path):
    def brain(body):
        return text("PASS") if talking(body) else call("set_price", price=JOINT)

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=2, cap=capability(channels=[PRIVATE]), private_pairs=True)
    seen = {(o.t, o.purpose): o.observation for o in of(sink, "observed", agent_id="A")}
    b_price = of(sink, "settled")[0].prices["B"]
    assert seen[(1, "talk")].rival_last_prices == {} and seen[(2, "talk")].rival_last_prices == {"B": b_price}
    assert seen[(2, "act")].rival_last_prices == {"B": b_price}
    talk2 = next(b for b in fake.bodies if talking(b) and b["messages"][-1]["content"].startswith("Round 2."))
    assert f"Rival prices last round: B {b_price:g}." in talk2["messages"][-1]["content"]
    blind, _, *_ = llm_run(brain, tmp_path / "opaque", rounds=2, cap=capability(channels=[PRIVATE], see_rivals=False), private_pairs=True)
    assert all(o.observation.rival_last_prices == {} for o in of(blind, "observed", agent_id="A")), "no grant, no rival prices"


def test_no_condition_has_queries_and_no_prompt_names_a_look_up_or_a_talk_tool(tmp_path):
    """DECISIONS-13 §1: max_queries_per_round 0 in all five conditions, so no text offers a look-up."""
    draft_conditions(tmp_path / "conditions")
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    with TestClient(create_app(tmp_path, use_dbos=False), base_url="http://t/api") as c:
        for cid in ("C1", "C2a", "C2b", "C3-accept", "C3-silent"):
            cond = c.get(f"conditions/{cid}").json()
            assert cond["limits"]["max_queries_per_round"] == 0, cid
            for variant in cond["prompt_variants"]:
                for seller, p in variant["sellers"].items():
                    assert "look-up" not in p["system_prompt"] and "look-up" not in (p["talk_prompt"] or ""), (cid, seller)
                    talk = p["talk_prompt"] or ""
                    assert "post" not in talk and "set_price" not in talk, "no talk text names a tool"


def test_a_text_reply_reaches_the_rival_next_round_and_the_judge_sees_the_posted_text(llm_client):  # noqa: F811
    c, said = llm_client, []
    line = "Message in private_A_B from {}: We could both stay at 10.5 from now on."

    def brain(body):
        user = body["messages"][1]["content"]
        if talking(body):
            if user.startswith("Round 1.") and not said:
                said.append(1)
                return text("  We could both stay at 10.5 from now on.  ")
            return text("PASS")
        read = body["messages"][-1]["role"] == "tool"
        if "New messages for you: 1." in user and "read_channel" in tools_offered(body) and not read:
            return call("read_channel", channel_id=PRIVATE)
        return call("set_price", price=10.5)

    c.fake.brain = brain
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    body = llm_config(pid)
    body["agents"][1]["policy"] = body["agents"][0]["policy"]
    rid = c.post("/api/runs", json=body).json()["run_ids"][0]
    c.app.state.svc.wait(rid, timeout=60)
    events = c.app.state.svc.db.events(rid)
    [post] = [e for e in events if e.type == "posted"]
    assert (post.t, post.channel_id, post.text) == (1, PRIVATE, "We could both stay at 10.5 from now on.")
    rival = "B" if post.agent_id == "A" else "A"
    talk2 = {e.agent_id: e.observation for e in events if e.type == "observed" and e.t == 2 and e.purpose == "talk"}
    assert [m.text for m in talk2[rival].new_messages] == [post.text] and talk2[post.agent_id].new_messages == []
    assert any(line.format(post.agent_id) in b["messages"][1]["content"] for b in c.fake.bodies if talking(b))
    [read] = [e for e in events if e.type == "tool_called" and e.tool == "read_channel"]
    assert (read.agent_id, read.t, read.allowed) == (rival, 2, True) and post.text in json.dumps(read.result)
    first = {e.agent_id: e.outcome for e in events if e.type == "talk_replied" and e.t == 1}
    assert first == {post.agent_id: "posted", rival: "pass"}
    view = render("talk_only", rid, events, [])
    assert [ln.text for ln in view.lines] == [post.text] and "PASS" not in view.text


def test_a_read_that_the_call_does_not_offer_is_a_budget_refusal_in_limits_hit(tmp_path):
    """CR-46 m3: a talk call offers no read, and an act call offers none after the one query; asking anyway is refused."""

    def brain(body):
        if talking(body):
            return text("PASS") if NO_LOOKUP in body["messages"][-1]["content"] else call("get_rival_prices", rounds=1)
        done = sum(m["role"] == "tool" for m in body["messages"])
        return call("get_news") if done < 2 else call("set_price", price=JOINT)

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=[PRIVATE], queries=1), private_pairs=True)
    called = [(e.tool, e.allowed) for e in of(sink, "tool_called", agent_id="A")]
    assert called == [("get_rival_prices", False), ("get_news", True), ("get_news", False)]
    assert not [c for c in of(sink, "llm_call") if c.error], "an unoffered granted read is not invalid_tool"
    refusals = [r for b in fake.bodies for r in tool_results(b) if isinstance(r, dict) and r.get("refused")]
    assert refusals and all(r["reason"] == NO_LOOKUP for r in refusals), "the act call (with tools) keeps tool messages"
    acted = of(sink, "acted", agent_id="A")[0]
    assert (acted.price, acted.fallback, acted.limits_hit) == (JOINT, False, ["capability.max_queries_per_round"]), "the binding limit"


def test_a_tool_that_is_not_granted_is_still_invalid(tmp_path):
    def brain(body):
        return call("read_channel", channel_id=PRIVATE) if not last_is_tool_result(body) else call("set_price", price=JOINT)

    sink, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(see_rivals=False))
    assert of(sink, "llm_call")[0].error == "invalid_tool" and not of(sink, "tool_called")


def test_new_runs_are_episode_v5_and_a_stored_v4_episode_still_loads(tmp_path):
    sink, *_ = llm_run(cooperator(), tmp_path, rounds=1)
    assert of(sink, "run_started")[0].header.schema_version == "episode.v5" == SCHEMA_VERSION
    lines = (ROOT / "tests" / "data" / "episode-pre-0.18.0.jsonl").read_text(encoding="utf-8").splitlines()
    header = EpisodeHeader.model_validate_json(lines[0])
    events = [SIM_EVENT.validate_json(x) for x in lines[1:]]
    assert header.schema_version == "episode.v4" and events[0].header.schema_version == "episode.v4"


def no_tool_shapes(body) -> None:
    """A request with no tools: no tool message, no assistant tool call, and alternating roles after the system prompt."""
    assert "tools" not in body and "tool_choice" not in body
    assert all(m["role"] != "tool" and not m.get("tool_calls") for m in body["messages"])
    roles = [m["role"] for m in body["messages"]]
    assert roles[0] == "system" and all(a != b for a, b in zip(roles[1:], roles[2:])), roles


def test_a_refused_read_in_a_talk_call_with_no_tools_is_sent_back_as_plain_user_text(tmp_path):
    """Lead, CR-46 open point 2: a follow-up request with no tools carries no tool call or tool message."""
    replies = iter([call("get_rival_prices", rounds=1), text("Shall we both hold 10.5?")])

    def brain(body):
        return next(replies) if talking(body) else call("set_price", price=JOINT)

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=[PRIVATE], queries=1), private_pairs=True)
    first, second = [b for b in fake.bodies if talking(b)]
    for body in (first, second):
        no_tool_shapes(body)
    assert [m["role"] for m in second["messages"]] == ["system", "user"]
    assert second["messages"][1]["content"] == first["messages"][1]["content"] + f"\n\nget_rival_prices: {NO_LOOKUP}"
    assert [(p.t, p.text) for p in of(sink, "posted")] == [(1, "Shall we both hold 10.5?")]
    assert not [c for c in of(sink, "llm_call") if c.error]


def test_assistant_text_beside_a_refused_read_is_kept_as_plain_text(tmp_path):
    replies = iter(
        [
            {**call("get_rival_prices", rounds=1), "content": "Let me check the rival first."},
            text("PASS"),
        ]
    )

    def brain(body):
        return next(replies) if talking(body) else call("set_price", price=JOINT)

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=[PRIVATE], queries=1), private_pairs=True)
    second = [b for b in fake.bodies if talking(b)][1]
    unposted = [(e.text, e.tier) for e in of(sink, "talk_text", agent_id="A")]
    assert unposted == [("Let me check the rival first.", "B"), ("PASS", "B")], "CR-46 n3: text beside a refused read"
    no_tool_shapes(second)
    assert second["messages"][2:] == [
        {"role": "assistant", "content": "Let me check the rival first."},
        {"role": "user", "content": f"get_rival_prices: {NO_LOOKUP}"},
    ]


def test_a_read_asked_for_in_a_talk_call_with_the_query_unused_is_not_a_limit_hit(tmp_path):
    """CR-46 n4: a talk call never offers reads; asking anyway is a refused record, not a query-limit hit."""

    def brain(body):
        if talking(body):
            return text("PASS") if NO_LOOKUP in body["messages"][-1]["content"] else call("get_news")
        return call("set_price", price=JOINT)

    sink, *_ = llm_run(brain, tmp_path, rounds=1, cap=capability(channels=[PRIVATE], queries=1), private_pairs=True)
    assert [(e.tool, e.allowed) for e in of(sink, "tool_called", agent_id="A")] == [("get_news", False)]
    acted = of(sink, "acted", agent_id="A")[0]
    assert (acted.price, acted.fallback, acted.limits_hit) == (JOINT, False, []), "the one query was never used"


def test_not_offered_reads_and_budget_refusals_are_counted_apart():
    budget = QueryBudget(limit=1)
    recorded = []
    box = ToolBox({"get_news": (TOOLS["get_news"], None)}, None, budget, lambda *a: recorded.append(a))
    box.refuse("get_news", {})
    assert (budget.not_offered, budget.refused) == (["get_news"], 0)
    budget.take()
    box.refuse("get_news", {})
    assert (budget.not_offered, budget.refused) == (["get_news"], 1)
    assert [r[0] for r in recorded] == ["get_news", "get_news"] and not any(r[3] for r in recorded)


def test_a_stored_v4_episode_re_hashes_to_its_stored_hash():
    """CR-50 addendum: tests/data/episode-pre-0.18.0.jsonl is a real episode.v4 (recorded at 8d9af94, no new fields)."""
    lines = (ROOT / "tests" / "data" / "episode-pre-0.18.0.jsonl").read_text(encoding="utf-8").splitlines()
    events = [SIM_EVENT.validate_json(x) for x in lines[1:]]
    observed = [e.observation for e in events if e.type == "observed"]
    assert observed and all(o.rival_last_prices is None and o.new_messages is None for o in observed)
    assert EpisodeHeader.model_validate_json(lines[0]).schema_version == "episode.v4"
    assert events[-1].type == "run_ended"
    assert Recorder(events[0].run_id, [], prior=events[:-1]).episode_hash == events[-1].episode_hash


def test_filled_observation_fields_are_in_the_hash_line(tmp_path):
    def brain(body):
        return text("PASS") if talking(body) else call("set_price", price=JOINT)

    sink, *_ = llm_run(brain, tmp_path, rounds=2, cap=capability(channels=[PRIVATE]), private_pairs=True)
    line = _hash_line(of(sink, "observed", agent_id="A")[0])
    assert '"rival_last_prices":{}' in line and '"new_messages":[]' in line, "empty but not null: hashed"


def test_a_provider_error_records_its_http_status_and_the_provider_code(tmp_path):
    sink, *_ = llm_run(lambda body: 500, tmp_path, rounds=1)
    calls = of(sink, "llm_call", agent_id="A")
    assert {(c.error, c.http_status, c.provider_code) for c in calls} == {("provider_error", 500, "server_error")}
    assert '"http_status":500' in _hash_line(calls[0]), "a filled field is hashed"


def test_a_network_failure_records_the_exception_class_and_no_status(tmp_path):
    sink, *_ = llm_run(lambda body: httpx2.ConnectError, tmp_path, rounds=1)
    calls = of(sink, "llm_call", agent_id="A")
    assert {(c.error, c.http_status, c.provider_code) for c in calls} == {("provider_error", None, "APIConnectionError")}


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"error": {"code": "rate_limit_exceeded", "message": "slow down"}}, "rate_limit_exceeded"),
        ({"code": 1305, "message": "busy"}, "1305"),
        ({"error": {"code": f"bad key {KEY}", "type": "Invalid Request"}}, None),
        ({"error": "free text"}, None),
    ],
)
def test_only_a_code_shaped_value_of_the_error_body_is_kept(body, code):
    transport = httpx2.MockTransport(lambda request: httpx2.Response(503, json=body))
    with pytest.raises(EndpointError) as e:
        Endpoint(PROFILE, KEY, transport=transport).request(Request([{"role": "user", "content": "u"}], [], True))
    assert (e.value.code, e.value.http_status, e.value.provider_code) == ("provider_error", 503, code)
    assert KEY not in repr(vars(e.value))


def ask_stream(choices):
    transport = httpx2.MockTransport(lambda request: streamed("openai_chat", {"id": "x", "created": 0, "model": "m", "choices": choices}))
    return Endpoint(PROFILE, KEY, transport=transport).request(Request([{"role": "user", "content": "u"}], [], True))


def test_an_unreadable_200_reply_records_the_exception_class():
    """A stream with no choice ends with no content: the library names it (CR-46 m4, DECISIONS-21)."""
    with pytest.raises(EndpointError) as e:
        ask_stream(None)
    assert (e.value.code, e.value.http_status, e.value.provider_code) == ("provider_error", None, "UnexpectedModelBehavior")


def test_a_streamed_choice_with_no_delta_is_an_empty_reply_and_without_a_finish_it_is_truncated():
    """On a stream a choice with no message is the same as an empty message: a reply with no text (outcome empty), as long
    as the provider finished it. With no finish reason the stream was cut (CR-85 1)."""
    reply, _ = ask_stream([{"index": 0, "finish_reason": "stop"}])
    assert (reply.text, reply.tool_calls, reply.thinking, reply.finish_reason) == (None, (), None, "stop")
    with pytest.raises(EndpointError) as e:
        ask_stream([{}])
    assert (e.value.code, e.value.provider_code) == ("provider_error", "stream_truncated")


def test_a_200_reply_with_no_choices_is_a_provider_error_and_the_seller_holds(tmp_path):
    sink, fake, *_ = llm_run(lambda body: NO_CHOICES, tmp_path, rounds=1)
    calls = of(sink, "llm_call", agent_id="A")
    assert [(c.attempt, c.error, c.http_status, c.provider_code) for c in calls] == [
        (n, "provider_error", None, "UnexpectedModelBehavior") for n in (1, 2, 3)
    ]
    acted = of(sink, "acted", agent_id="A")[0]
    assert acted.fallback and acted.hold
    assert of(sink, "run_ended")[0].status == "completed"


def test_a_stored_llm_episode_before_0_22_1_re_hashes_to_its_stored_hash():
    """tests/data/episode-pre-0.22.1-llm.jsonl is the PILOT-1c C1b episode (episode.v5, 1833e3c), with provider_error calls
    stored before llm_call had http_status and provider_code. Counts and hashes only; no post and no key in it."""
    lines = (ROOT / "tests" / "data" / "episode-pre-0.22.1-llm.jsonl").read_text(encoding="utf-8").splitlines()
    events = [SIM_EVENT.validate_json(x) for x in lines[1:]]
    errors = [e for e in events if e.type == "llm_call" and e.error == "provider_error"]
    assert errors and all(e.http_status is None and e.provider_code is None for e in errors)
    assert EpisodeHeader.model_validate_json(lines[0]).schema_version == "episode.v5"
    assert events[-1].type == "run_ended"
    assert Recorder(events[0].run_id, [], prior=events[:-1]).episode_hash == events[-1].episode_hash
