# ruff: noqa: F401, F811 (the fixture fake_client comes from test_d13)
"""DECISIONS-15 §1: each call shows the whole episode so far, as far as the seller may see it; no notes in research runs;
one system and one user message per call."""

from services import estimate as est
from services.configs import RunConfig
from simcore.state import Message, NewsItem, Outcome, PricePoint, RunState
from simcore.view import MAX_MESSAGES, MAX_NEWS, View
from simkit import capability
from simschema.events import ChannelInfo
from test_d13 import fake_client, run_condition, talker
from test_profiles import profile_body
from test_research import research_config

CH = "private_A_B"
ROUNDS = 30  # over every old cap: 6 messages, 5 news items, 5 own rounds, 5 rival rounds


def long_state() -> RunState:
    return RunState(
        history={s: [PricePoint(t=t, price=5 + t / 10 + (s == "B")) for t in range(1, ROUNDS + 1)] for s in "AB"},
        outcomes={s: [Outcome(t=t, quantity=4.0, profit=10.0 + t) for t in range(1, ROUNDS + 1)] for s in "AB"},
        messages=[
            Message(id=2 * t + (s == "B"), t=t, channel_id=CH, sender=s, text=f"{s} says {t}", deliver_t=t + 1)
            for t in range(1, ROUNDS + 1)
            for s in "AB"
        ],
        news=[NewsItem(t=t, event_index=t, text=f"news {t}", visible=["A", "B"]) for t in range(1, ROUNDS + 1)],
    )


def view(see_rivals=True, c3_isolation=False) -> View:
    caps = {s: capability([CH], see_rivals=see_rivals) for s in "AB"}
    return View([ChannelInfo(id=CH, kind="private", participants=["A", "B"])], caps, c3_isolation)


def test_a_late_round_observation_holds_the_whole_record_from_round_1():
    t = ROUNDS + 1
    state = long_state()
    o = view().observe("A", t, state, "talk")
    assert [(m.sender, m.t) for m in o.recent_messages] == [(s, r) for r in range(1, ROUNDS + 1) for s in "AB"], "every message"
    assert [n.t for n in o.news] == list(range(1, ROUNDS + 1)), "every news item"
    assert [r.t for r in o.own_history] == list(range(1, ROUNDS + 1)), "every own round"
    assert o.own_history[-1].profit == o.own.last_profit
    assert list(o.rival_history) == ["B"] and [(p.t, p.price) for p in o.rival_history["B"]] == [
        (p.t, p.price) for p in state.history["B"]
    ], "every rival round"
    assert o.rival_history["B"][-1].price == o.rival_last_prices["B"]
    assert len(o.recent_messages) > MAX_MESSAGES and len(o.news) > MAX_NEWS, "the record is over the read tools' caps"


def test_the_read_tools_keep_their_caps():
    v, state = view(), long_state()
    assert len(v.messages("A", CH, ROUNDS + 1, state)) == MAX_MESSAGES
    assert len(v.news("A", ROUNDS + 1, state)) == MAX_NEWS
    assert len(v.rival_prices("A", ROUNDS + 1, 50, state)["B"]) == 5 and len(v.own_sales("A", ROUNDS + 1, 50, state)) == 5


def test_rival_history_needs_the_get_rival_prices_grant():
    o = view(see_rivals=False).observe("A", ROUNDS + 1, long_state(), "act")
    assert o.rival_history == {} and not o.rival_last_prices


def test_a_c3_act_call_sees_no_message():
    o = view(c3_isolation=True).observe("A", ROUNDS + 1, long_state(), "act")
    assert o.recent_messages == [] and o.new_messages == []


def test_a_research_run_writes_no_notes_and_sends_one_system_and_one_user_message(fake_client):
    fake_client.fake.brain = talker
    events, cond = run_condition(fake_client, "C1")
    assert cond["memory_summary"] == "none"
    assert max(e["t"] for e in events) > 5, "longer than summary_every, so the old rule would write a note"
    assert not [e for e in events if e["type"] == "memory_summarised"]
    bodies = fake_client.fake.bodies
    assert bodies and all([m["role"] for m in b["messages"]] == ["system", "user"] for b in bodies), "no earlier reply resent"
    assert not any("Your notes" in b["messages"][1]["content"] for b in bodies)


def test_a_research_run_takes_memory_summary_from_its_condition_and_refuses_another_value(fake_client):
    for memory, status in (({}, 202), ({"summary_every": 3}, 202), ({"summary": "none"}, 202), ({"summary": "code"}, 422)):
        r = fake_client.post("/api/runs", json={**research_config("C1", seeds=(1,)), "memory": memory})
        assert r.status_code == status, (memory, r.json())
        if status == 422:
            assert r.json()["code"] == "research_field_locked" and r.json()["errors"][0]["path"] == "memory/summary"


def test_the_worst_case_estimate_grows_with_the_record(fake_client):
    pid = fake_client.post("/api/provider-profiles", json=profile_body()).json()["id"]
    body = research_config("C1", seeds=(1,))
    for a in body["agents"]:
        a["policy"] = {"kind": "llm", "llm": {"provider_profile_id": pid, "model": "m-small", "model_family": "fake"}}
    (seed_run,) = fake_client.app.state.research.expand(RunConfig.model_validate(body))
    cfg = seed_run.config
    # two sellers, each posts at most once per round on one channel: 2 lines, the news, 2 posts at full length
    assert est.history_chars(cfg) == 2 * est.LINE_CHARS + est.NEWS_CHARS + 2 * (est.POST_CHARS + est.LINE_CHARS)
    one, two, late = (est.totals(est.run_usage(cfg, n, first))[1] for n, first in ((1, 1), (2, 1), (1, 10)))
    assert two > 2 * one and late > one, "later rounds cost more input"
    assert est.totals(est.run_usage(cfg, 2))[0] == 2 * est.totals(est.run_usage(cfg, 1))[0], "the calls do not change"


def test_a_condition_file_must_state_memory_summary():
    import pytest
    import yaml
    from pydantic import ValidationError

    from services.research import ConditionFile
    from test_api import ROOT

    c1 = yaml.safe_load((ROOT / "conditions" / "C1.yaml").read_text(encoding="utf-8"))
    assert ConditionFile.model_validate(c1).memory_summary == "none"
    with pytest.raises(ValidationError, match="memory_summary"):
        ConditionFile.model_validate({k: v for k, v in c1.items() if k != "memory_summary"})
