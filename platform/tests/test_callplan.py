"""The call plan (QUEUE rule b; DE-callplan-report.md; CR-78 invariants). The expected values are predicates written from
the decisions (CR-78 §2), not read from the code. The plan product is enumerated in full; the value checks run the real
View, the real LLM policy on a fake endpoint, and the estimate."""

import itertools
import json
import time
from pathlib import Path

import pytest
import yaml

from fakellm import call, text, tools_offered
from services.research import ConditionFile
from simcore.callplan import NO_TALK_UNDER_C3, Setting, call_plan, open_channels
from simcore.state import Mask, Message, NewsItem, Outcome, PricePoint, RunState
from simcore.view import QueryBudget, View
from simkit import of
from simschema.events import Capability, RunLimits, ToolGrant
from test_llm_policy import llm_run

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ("get_news", "get_rival_prices", "get_own_sales", "get_price_history", "read_channel")
OTHERS = ("calc_profit", "write_note", "read_note")
OPTIONAL = ("get_news", "get_rival_prices", "get_own_sales", "get_price_history", "calc_profit", "write_note", "read_note", "hold")
CHANNEL_OPTIONS = (None, ("public",), ("private_A_B",), ("public", "private_A_B"), ())
SHAPES = (("one_call", False), ("talk_and_act", False), ("talk_and_act", True))
MODES = ("push", "pull", "both")
PURPOSES = ("talk", "act", "one_call")


def cap(read=None, post=None, optional=OPTIONAL, queries=3) -> Capability:
    grants = [ToolGrant(tool_id="set_price"), *(ToolGrant(tool_id=t) for t in optional)]
    if read is not None:
        grants.append(ToolGrant(tool_id="read_channel", params={"channels": list(read)}))
    if post is not None:
        grants.append(ToolGrant(tool_id="post", params={"channels": list(post)}))
    return Capability(grants=grants, max_queries_per_round=queries)


B_CAP = cap(("public", "private_A_B"), ("public", "private_A_B"), queries=1)
"""Seller B: every channel, and a query cap other than A's, so a limit that leaks from one seller to the other shows."""


def setting(a: Capability, shape=SHAPES[1], mode="both", limit=3, public=True, pairs=True, news=True, opaque=False, memory="code", b=B_CAP):
    call_mode, c3 = shape
    return Setting(
        capabilities={"A": a, "B": b},
        channels=open_channels(["A", "B"], public, pairs),
        call_mode=call_mode,
        c3_isolation=c3,
        observation_mode=mode,
        limits=RunLimits(max_queries_per_round=limit),
        memory_summary=memory,
        news=news,
        transparency="opaque" if opaque else "rival_last_round",
    )


def expected_open(names, s: Setting) -> list[str]:
    """CR-78 I1: a channel stays in a grant when the run opens it (both sellers of this test are in every channel)."""
    opened = {c.id for c in s.channels}
    return [c for c in names or () if c in opened]


def check_seller(sid: str, raw: Capability, s: Setting) -> None:
    """Every CR-78 invariant that is a predicate of the setting alone, for one seller."""
    p = call_plan(s).sellers[sid]
    granted = {g.tool_id for g in raw.grants}
    off = ({"get_news"} if not s.news else set()) | ({"get_rival_prices"} if s.transparency == "opaque" else set())
    read = expected_open(raw.grant("read_channel") and raw.grant("read_channel").params["channels"], s)
    post = expected_open(raw.grant("post") and raw.grant("post").params["channels"], s)
    effective = (granted - off - {"read_channel", "post"}) | ({"read_channel"} if read else set()) | ({"post"} if post else set())
    # I1 effective grants, recorded in the header
    assert {g.tool_id for g in p.capability.grants} == effective
    for g in p.capability.grants:
        if g.tool_id in ("read_channel", "post"):
            assert g.params["channels"] == (read if g.tool_id == "read_channel" else post)
    limit = min(raw.max_queries_per_round, s.limits.max_queries_per_round)
    assert p.query_limit == limit
    # R9 (b), I6: pull with no query is refused
    assert any("pull needs a query limit" in r for r in p.refused) == (s.observation_mode == "pull" and limit == 0)
    # I2 talk turn; I13 calls per round
    talks = s.call_mode == "talk_and_act" and bool(post)
    assert set(p.calls) == ({"one_call"} if s.call_mode == "one_call" else {"act"} | ({"talk"} if talks else set()))
    # Notes (reported, not refused): C3-silent's shape (CR-79 R9 d), and D13 §1: a seller that sees only what is pushed
    # (push, or no query) but lacks the grant for a part
    notes = [NO_TALK_UNDER_C3] if s.c3_isolation and read and not talks else []
    if s.observation_mode == "push" or limit == 0:
        gaps = ["own price, sales and profit"] if "get_own_sales" not in effective else []
        gaps += ["news"] if s.news and "get_news" not in effective else []
        gaps += ["channel messages"] if s.channels and "post" in effective and "read_channel" not in effective else []
        notes += [f"seller {sid} has no queries and cannot see: {', '.join(gaps)}"] if gaps else []
    assert list(p.notes) == notes
    push_plan = call_plan(Setting(**{**s.__dict__, "observation_mode": "push"})).sellers[sid]
    for purpose, c in p.calls.items():
        isolated = s.c3_isolation and purpose == "act"
        sees = [] if isolated else read
        # I3 gates, I4 mode
        gates = {"own": "get_own_sales" in effective}
        if s.observation_mode != "pull":
            gates |= {"rival_prices": "get_rival_prices" in effective, "news": "get_news" in effective, "messages": bool(sees)}
        assert c.record == {g for g, on in gates.items() if on}, (purpose, c.record)
        assert c.record <= push_plan.calls[purpose].record
        assert list(c.readable) == sees
        # I5 offer rule, I6 budget
        if s.observation_mode == "push" or limit == 0:
            assert c.reads == c.offered == ()
        else:
            assert set(c.reads) == {t for t in QUERIES if t in effective and (t != "read_channel" or sees)}
            talk_reads = {"read_channel"} & set(c.reads) if s.observation_mode == "pull" else set()
            assert set(c.offered) == (talk_reads if purpose == "talk" else set(c.reads))
        assert set(c.other_tools) == {t for t in OTHERS if t in effective}
        assert set(c.offered) <= effective and set(c.writes) <= effective
        # R7 writes
        text_channel = post[0] if purpose == "talk" and len(post) == 1 else None
        assert c.text_channel == text_channel
        want = {"talk": {"post"}, "act": {"set_price", "hold"}, "one_call": {"post", "set_price", "hold"}}[purpose]
        want = {w for w in want if w in effective and (w != "post" or (post and not text_channel))}
        assert set(c.writes) == want
        if purpose == "talk":
            assert c.writes or c.text_channel  # I2: a talk call can post
        # I7 isolation
        if isolated:
            assert "messages" not in c.record and "read_channel" not in c.reads and c.notes_from == {"act"}
        else:
            assert c.notes_from == set(PURPOSES)
        # R4b (a): the memory summary does not depend on a grant
        assert c.summary == (s.memory_summary != "none")


def run_axes():
    return itertools.product(SHAPES, MODES, (0, 1, 2), (False, True), (False, True), (False, True), (False, True), ("none", "code", "llm"))


def test_every_setting_of_the_run_and_the_channel_grants_holds_the_invariants():
    """32,400 settings (the run axes x seller A's read and post grants); both sellers checked in each (CR-78 §3.4)."""
    started, n = time.perf_counter(), 0
    for (shape, mode, limit, public, pairs, news, opaque, memory), read, post in itertools.product(
        run_axes(), CHANNEL_OPTIONS, CHANNEL_OPTIONS
    ):
        a = cap(read, post)
        s = setting(a, shape, mode, limit, public, pairs, news, opaque, memory)
        check_seller("A", a, s)
        check_seller("B", B_CAP, s)
        n += 1
    assert n == 3 * 3 * 3 * 2**4 * 3 * 5 * 5
    assert time.perf_counter() - started < 120


def test_every_subset_of_the_other_grants_holds_the_invariants():
    """2^8 subsets of A's other grants x call shape x mode x query limit."""
    for k in range(len(OPTIONAL) + 1):
        for subset in itertools.combinations(OPTIONAL, k):
            for shape, mode, limit in itertools.product(SHAPES, MODES, (0, 1, 2)):
                a = cap(("private_A_B",), ("private_A_B",), subset)
                check_seller("A", a, setting(a, shape, mode, limit))


def test_one_call_with_isolation_is_refused():
    assert "c3_isolation needs call_mode talk_and_act" in call_plan(setting(B_CAP, ("one_call", True))).refused


@pytest.mark.parametrize(
    "grant, refused",
    [
        (("typo",), "names ['typo']"),
        (("private_A_D",), "names ['private_A_D']"),
        (("private_B_C",), "names ['private_B_C'], which is not public or a private pair of A"),
    ],
)
def test_a_channel_that_cannot_exist_or_is_not_the_sellers_is_refused(grant, refused):
    """R9 (c), CR-79 M1: possible channels are public and the pairs of the run's sellers. B_C is a pair of a 3-seller run
    that A is not in."""
    a = cap(grant, grant)
    s = Setting({"A": a, "B": B_CAP, "C": B_CAP}, open_channels(["A", "B", "C"], True, True))
    assert any(refused in r for r in call_plan(s).sellers["A"].refused)


def test_a_possible_but_closed_channel_is_trimmed_not_refused():
    """CR-79 M1: the default grant private_A_B with private_pairs off."""
    a = cap(("private_A_B",), ("private_A_B",))
    p = call_plan(setting(a, pairs=False)).sellers["A"]
    assert p.refused == () and p.capability.grant("post") is None and set(p.calls) == {"act"}


def test_a_seller_without_set_price_is_refused():
    a = Capability(grants=[ToolGrant(tool_id="hold")])
    assert "seller A has no set_price grant" in call_plan(setting(a)).refused


def off_diff(a: Capability, tool: str, s: Setting) -> None:
    """CR-78 I11: switching one of A's grants off changes nothing of B's plan, and of A's plan only that tool and what
    follows from it."""
    less = a.model_copy(update={"grants": [g for g in a.grants if g.tool_id != tool]})
    before, after = call_plan(s), call_plan(Setting(**{**s.__dict__, "capabilities": {"A": less, "B": s.capabilities["B"]}}))
    assert after.sellers["B"] == before.sellers["B"]
    old, new = before.sellers["A"], after.sellers["A"]
    assert new.query_limit == old.query_limit
    if tool == "post":
        assert set(new.calls) == set(old.calls) - {"talk"}
    else:
        assert set(new.calls) == set(old.calls)
    group = {"get_own_sales": "own", "get_rival_prices": "rival_prices", "get_news": "news", "read_channel": "messages"}.get(tool)
    for purpose, c in new.calls.items():
        o = old.calls[purpose]
        assert c.record == o.record - {group}
        assert c.reads == tuple(t for t in o.reads if t != tool)
        assert c.offered == tuple(t for t in o.offered if t != tool)
        assert c.other_tools == tuple(t for t in o.other_tools if t != tool)
        if tool == "read_channel":
            assert c.readable == ()
        else:
            assert c.readable == o.readable
        if tool == "post":
            assert c.postable == () and c.text_channel is None and "post" not in c.writes
        else:
            assert (c.postable, c.text_channel) == (o.postable, o.text_channel)
            assert c.writes == tuple(w for w in o.writes if w != tool)


def test_switching_one_grant_off_changes_only_that_grant_for_that_seller():
    for shape, mode, limit, public, pairs, news, opaque in itertools.product(
        SHAPES, MODES, (0, 1, 2), (False, True), (False, True), (False, True), (False, True)
    ):
        for read, post in ((("public", "private_A_B"),) * 2, (("private_A_B",), None), (None, ("public",))):
            a = cap(read, post)
            s = setting(a, shape, mode, limit, public, pairs, news, opaque)
            for tool in {g.tool_id for g in a.grants} - {"set_price"}:
                off_diff(a, tool, s)
            fewer = a.model_copy(update={"max_queries_per_round": 0})
            changed = call_plan(Setting(**{**s.__dict__, "capabilities": {"A": fewer, "B": B_CAP}}))
            assert changed.sellers["B"] == call_plan(s).sellers["B"]  # A's query cap is A's alone


def test_the_plan_is_a_pure_function_of_the_setting():
    s = setting(cap(("private_A_B",), ("public", "private_A_B")), mode="pull", limit=1)
    assert call_plan(s) == call_plan(s)


def test_the_five_conditions_pass_and_c3_silent_b_carries_the_note():
    for path in sorted((ROOT / "conditions").glob("C*.yaml")):
        f = ConditionFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        plan = f.plan()
        assert plan.refused == ()
        notes = {sid: p.notes for sid, p in plan.sellers.items()}
        assert notes == ({"A": (), "B": (NO_TALK_UNDER_C3,)} if f.id == "C3-silent" else {sid: () for sid in notes}), f.id


# Value checks: the real View, policy and estimate follow the plan (CR-79 M4)

SENTINEL = {"news": "SENT-NEWS", "channel": "SENT-CHAN", "rival": 7.77}


def sentinel_state(rounds: int = 3) -> RunState:
    state = RunState()
    for r in range(1, rounds + 1):
        state.history.setdefault("A", []).append(PricePoint(t=r, price=6.0))
        state.history.setdefault("B", []).append(PricePoint(t=r, price=SENTINEL["rival"]))
        for s in ("A", "B"):
            state.outcomes.setdefault(s, []).append(Outcome(t=r, quantity=8.0, profit=40.0))
        state.messages.append(Message(id=r, t=r, channel_id="private_A_B", sender="B", text=f"{SENTINEL['channel']}-{r}", deliver_t=r + 1))
        state.news.append(NewsItem(t=r, event_index=r, text=f"{SENTINEL['news']}-{r}", visible=["A", "B"]))
    return state


def run_view(s: Setting) -> View:
    """The View of a run of this setting: built, like Run, from the header's effective grants."""
    header = {sid: p.capability for sid, p in call_plan(s).sellers.items()}
    return View(list(s.channels), header, s.c3_isolation, mode=s.observation_mode, call_mode=s.call_mode, limits=s.limits)


def everything_a_call_sees(view: View, purpose: str, t: int, state: RunState) -> str:
    """The observation, the request text and the result of every offered read and other tool."""
    from adapters.llm.policy import render

    obs = view.observe("A", t, state, purpose)
    box = view.toolbox("A", t, state, purpose, QueryBudget(9), lambda *a: None)
    spec = view.call("A", purpose)
    args = {"read_channel": {"channel_id": "private_A_B"}, "calc_profit": {"price": 6.0}, "write_note": {"text": "x"}, "read_note": {}}
    args |= {"get_rival_prices": {"rounds": 9}, "get_own_sales": {"rounds": 9}, "get_price_history": {"rounds": 9}, "get_news": {}}
    results = [box.call(tool, **args[tool]) for tool in spec.offered + spec.other_tools]
    assert None not in results  # every offered tool ran
    blob = obs.model_dump_json() + render(obs, ["summary"], purpose, list(spec.postable), bool(spec.offered), "A")
    return blob + json.dumps(results, default=str)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("shape", SHAPES[1:])
@pytest.mark.parametrize("news, opaque", [(True, False), (False, True)])
def test_a_hidden_item_never_reaches_a_call(mode, shape, news, opaque):
    """CR-78 I3, I7, I9: plant a sentinel in each item; it is absent from everything a call sees when its gate is closed,
    and present in a push or both observation when the plan's record holds it."""
    a = cap(("private_A_B",), ("private_A_B",))
    s = setting(a, shape, mode, 3, False, True, news, opaque)
    view = run_view(s)
    state = sentinel_state()
    plan = view.plan.sellers["A"]
    for purpose, c in plan.calls.items():
        seen = everything_a_call_sees(view, purpose, 4, state)
        isolated = s.c3_isolation and purpose == "act"
        assert (SENTINEL["news"] in seen) == (news and ("news" in c.record or "get_news" in c.offered))
        assert (str(SENTINEL["rival"]) in seen) == (
            not opaque and ("rival_prices" in c.record or bool(set(c.offered) & {"get_rival_prices", "get_price_history"}))
        )
        assert (SENTINEL["channel"] in seen) == (not isolated and ("messages" in c.record or "read_channel" in c.offered))
        if isolated:
            assert SENTINEL["channel"] not in seen


def test_price_history_under_opaque_shows_only_own_prices():
    a = cap(None, None)
    s = setting(a, mode="both", opaque=True)
    view = run_view(s)
    box = view.toolbox("A", 4, sentinel_state(), "act", QueryBudget(3), lambda *a: None)
    assert "get_price_history" in view.call("A", "act").offered
    assert set(box.call("get_price_history", rounds=9)) == {"A"}


@pytest.mark.parametrize("mask", ["clear", "shuffle", "noop"])
def test_a_mask_is_the_same_in_push_and_in_pull(mask):
    """CR-78 I8: the observation (push), the read_channel result (pull) and the request text show the same masked texts."""
    state = sentinel_state(4)
    if mask == "noop":
        state.messages = [m.model_copy(update={"text": "same"}) for m in state.messages]
    state.masks.append(Mask(channel_id="private_A_B", mode="shuffle" if mask == "noop" else mask, from_t=3))
    a = cap(("private_A_B",), ("private_A_B",))
    texts = {}
    for mode in ("push", "pull"):
        view = View([*open_channels(["A", "B"], False, True)], {"A": a, "B": a}, False, mode=mode)
        if mode == "push":
            obs = view.observe("A", 5, state, "act")
            texts[mode] = [m.text for m in obs.recent_messages]
            noop = obs.mask_noop
        else:
            box = view.toolbox("A", 5, state, "act", QueryBudget(3), lambda *a: None)
            texts[mode] = [m["text"] for m in box.call("read_channel", channel_id="private_A_B")]
    assert texts["push"] == texts["pull"]
    original = [m.text for m in state.messages if m.deliver_t <= 5]
    if mask == "clear":
        assert texts["push"] == []
    elif mask == "shuffle":
        assert sorted(texts["push"]) == sorted(original) and texts["push"] != original
    else:
        assert texts["push"] == original and noop is True


def test_push_shows_the_whole_record_above_the_old_caps():
    """D15 §1: 24 messages and 24 news items at round 25 (the old caps were 20 and 5)."""
    a = cap(("private_A_B",), ("private_A_B",))
    view = View([*open_channels(["A", "B"], False, True)], {"A": a, "B": a}, False, mode="push")
    obs = view.observe("A", 25, sentinel_state(24), "act")
    assert len(obs.recent_messages) == 24 and len(obs.news) == 24 and len(obs.own_history) == 24


def first_requests(sink, fake) -> dict[str, list[str]]:
    """The tools offered in each purpose's first request of round 1 (one request per call: the brain never reads)."""
    calls = [c for c in of(sink, "llm_call", agent_id="A")]
    out = {}
    for c, body in zip(calls, fake.bodies):
        if c.t == 1:
            out.setdefault(c.purpose, tools_offered(body))
    return out


def silent(body):
    offered = tools_offered(body)
    if "set_price" in offered:
        return call("set_price", price=6.0)
    return call("hold") if "hold" in offered else text("PASS")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("limit", [1, 2])
@pytest.mark.parametrize(
    "read, post", [(("private_A_B",), ("private_A_B",)), (("private_A_B",), None), (("private_A_B",), ("public", "private_A_B"))]
)
def test_the_real_request_offers_exactly_the_plans_tools(tmp_path, mode, shape, limit, read, post):
    """CR-78 I5, I9 on the real LLM policy: the first request of each call offers the plan's reads (a query is left), the
    other tools, then the writes; and the run makes exactly the plan's calls."""
    a = cap(read, post, queries=limit)
    call_mode, c3 = shape
    kw = dict(public=True, private_pairs=True, call_mode=call_mode, c3_isolation=c3, observation_mode=mode, memory_summary="none")
    sink, fake, *_ = llm_run(silent, tmp_path, rounds=2, cap=a, **kw)
    plan = call_plan(setting(a, shape, mode, 3, True, True, b=a)).sellers["A"]
    got = first_requests(sink, fake)
    assert set(got) == set(plan.calls)
    for purpose, c in plan.calls.items():
        assert got[purpose] == [*c.offered, *c.other_tools, *c.writes], purpose


def test_pull_with_one_query_the_talk_read_uses_it_and_the_act_read_is_refused(tmp_path):
    """CR-79 M2: talk and act share the budget."""

    def reader(body):
        offered = tools_offered(body)
        if body["messages"][-1]["role"] == "tool" or "read_channel" not in offered and "get_news" not in offered:
            return silent(body)
        return call("read_channel", channel_id="private_A_B") if "read_channel" in offered else call("get_news")

    a = cap(("private_A_B",), ("private_A_B",), queries=1)
    sink, fake, *_ = llm_run(
        reader, tmp_path, rounds=2, cap=a, public=False, private_pairs=True, observation_mode="pull", memory_summary="none"
    )
    first = [e for e in of(sink, "tool_called", agent_id="A") if e.t == 1]
    assert [(e.tool, e.allowed) for e in first if e.tool == "read_channel"] == [("read_channel", True)]
    act = [b for c, b in zip(of(sink, "llm_call", agent_id="A"), fake.bodies) if c.t == 1 and c.purpose == "act"]
    assert all(not set(tools_offered(b)) & set(QUERIES) for b in act)


def test_the_estimate_counts_the_plans_calls():
    """CR-78 I13: a post grant on a closed channel gives no talk call, so no talk turn in the estimate."""
    from services.configs import RunConfig
    from services.estimate import turns
    from test_api import config

    def llm_config(post, public):
        body = config()
        a = body["agents"][0]
        a["policy"] = {"kind": "llm", "llm": {"provider_profile_id": "prof", "model": "m"}}
        a["capability"] = cap(None, post).model_dump(mode="json")
        return RunConfig.model_validate({**body, "channels": {"public": public, "private_pairs": True}})

    closed = llm_config(post=("public",), public=False)
    none = llm_config(post=None, public=False)
    opened = llm_config(post=("public",), public=True)
    assert turns(closed) == turns(none) == {"talk": 0, "act": 1, "one_call": 0}
    assert turns(opened) == {"talk": 1, "act": 1, "one_call": 0}
