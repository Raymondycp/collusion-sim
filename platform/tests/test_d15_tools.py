"""DECISIONS-15 §3: observation_mode push|pull|both, the optional tools (calc_profit, get_price_history, write_note,
read_note) as pure functions on the run state behind their grants, and the freeze check of §3.4. Scripted and fake
endpoint only."""

import ast
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from fakellm import call, cooperator, text, tools_offered
from services.research import ConditionFile, setting_problems
from simcore.agents import PolicyContext
from simcore.recorder import MemorySink, Recorder
from simcore.run import Run
from simcore.scenario import Event
from simcore.scripted import make_scripted
from simcore.shocks import Shock
from simcore.state import Message, NewsItem, Outcome, PricePoint, RunState
from simcore.view import PULL_KEEPS, QueryBudget, View, result_hash
from simkit import JOINT, capability, frozen_scenario, of, spec
from simschema.events import TIER, ChannelInfo, Observation, ToolGrant
from test_freeze import client, freeze, root, rubric_file  # noqa: F401 (fixtures)
from test_llm_policy import llm_run
from test_research import ROOT, research  # noqa: F401 (fixture)

NEW = ("calc_profit", "get_price_history", "write_note", "read_note")
CHANNEL = ChannelInfo(id="private_A_B", kind="private", participants=["A", "B"])


def every_grant(queries=3, extra=NEW):
    cap = capability(channels=("private_A_B",), queries=queries)
    return cap.model_copy(update={"grants": [*cap.grants, *(ToolGrant(tool_id=t) for t in extra)]})


def rich_state(rounds=8) -> RunState:
    state = RunState()
    for r in range(1, rounds + 1):
        for s, p in (("A", 6.0), ("B", 7.0)):
            state.history.setdefault(s, []).append(PricePoint(t=r, price=p))
            state.outcomes.setdefault(s, []).append(Outcome(t=r, quantity=8.0, profit=40.0))
        state.messages.append(Message(id=r, t=r, channel_id="private_A_B", sender="B", text=f"m{r}", deliver_t=r + 1))
        state.news.append(NewsItem(t=r, event_index=r, text=f"n{r}", visible=["A", "B"]))
    state.news.append(NewsItem(t=rounds + 1, event_index=rounds + 1, text="now", visible=["A", "B"]))
    return state


def view(mode, cap=None, c3=False) -> View:
    cap = cap or every_grant()
    return View([CHANNEL], {"A": cap, "B": cap}, c3, mode=mode)


def empty(name: str, value) -> bool:
    field = Observation.model_fields[name]
    return value == (type(value)() if field.is_required() else field.get_default(call_default_factory=True))


# §3.2 observation_mode


def test_pull_keeps_only_the_round_and_the_own_last_result():
    state = rich_state()
    both = view("both").observe("A", 9, state, "act")
    pull = view("pull").observe("A", 9, state, "act")
    for name in PULL_KEEPS:
        assert getattr(pull, name) == getattr(both, name)
    assert both.own.last_price == 6.0
    full = {n for n in Observation.model_fields if n not in PULL_KEEPS and not empty(n, getattr(both, n))}
    assert {"new_message_count", "headlines", "rival_last_prices", "new_messages", "news", "recent_messages", "own_history"} <= full
    for name in Observation.model_fields:
        if name not in (*PULL_KEEPS, "masked"):
            assert empty(name, getattr(pull, name)), f"pull mode pushes {name}"
    assert pull.masked == ["channels", "news"]


def test_push_offers_no_read_of_the_record_and_keeps_calc_profit_and_the_notes():
    state = rich_state()
    offered = {
        mode: [s.id for s in view(mode).toolbox("A", 9, state, "act", QueryBudget(3), lambda *a: None).specs]
        for mode in ("push", "pull", "both")
    }
    reads = ["get_news", "get_own_sales", "get_rival_prices", "read_channel", "get_price_history"]
    assert offered["push"] == ["calc_profit", "write_note", "read_note"]
    assert set(offered["pull"]) == set(offered["both"]) == {*reads, "calc_profit", "write_note", "read_note"}


@pytest.mark.parametrize("tool", NEW)
def test_each_new_tool_is_in_the_call_only_when_granted(tool):
    state = rich_state()
    granted = view("both", every_grant(extra=(tool,))).toolbox("A", 9, state, "act", QueryBudget(3), lambda *a: None)
    others = view("both", every_grant(extra=[t for t in NEW if t != tool])).toolbox("A", 9, state, "act", QueryBudget(3), lambda *a: None)
    assert granted.has(tool) and not others.has(tool)
    assert others.call(tool) is None


def test_a_pull_run_needs_a_query_limit():
    cap = every_grant(queries=0)
    with pytest.raises(ValidationError, match="observation_mode pull needs a query limit above 0"):
        spec({"A": cap, "B": cap}, {"A": "constant", "B": "constant"}, observation_mode="pull")
    assert spec({"A": cap, "B": cap}, {"A": "constant", "B": "constant"}, observation_mode="push").observation_mode == "push"


def test_every_condition_states_its_mode_and_pull_needs_queries():
    c1 = yaml.safe_load((ROOT / "conditions" / "C1.yaml").read_text(encoding="utf-8"))
    assert ConditionFile.model_validate(c1).observation_mode == "push"
    with pytest.raises(ValidationError, match="observation_mode"):
        ConditionFile.model_validate({k: v for k, v in c1.items() if k != "observation_mode"})
    with pytest.raises(ValidationError, match="observation_mode pull needs a query limit above 0"):
        ConditionFile.model_validate({**c1, "observation_mode": "pull"})
    ConditionFile.model_validate({**c1, "observation_mode": "pull", "limits": {**c1["limits"], "max_queries_per_round": 2}})


# The request the model receives


def act_bodies(fake):
    return [b for b in fake.bodies if "set_price" in tools_offered(b)]


def user_text(body) -> str:
    return next(m["content"] for m in body["messages"] if m["role"] == "user")


def test_in_pull_mode_the_request_holds_the_round_and_the_own_result_and_offers_the_reads(tmp_path):
    _, both, _, _ = llm_run(cooperator(), tmp_path / "both", rounds=3)
    sink, pull, _, _ = llm_run(cooperator(), tmp_path / "pull", rounds=3, observation_mode="pull")
    assert "Rival prices last round" in user_text(act_bodies(both)[-1])
    for body in act_bodies(pull):
        lines = user_text(body).splitlines()
        assert re.fullmatch(r"Round \d+\.", lines[0])
        assert all(re.match(r"(Last round: your price|Use the tools)", line) for line in lines[1:]), lines
        assert {"get_rival_prices", "get_own_sales", "get_news"} <= set(tools_offered(body))
    looked = of(sink, "tool_called", agent_id="A", tool="get_rival_prices")
    assert looked and all(e.allowed for e in looked)


def test_in_push_mode_no_read_is_offered_even_with_a_query_limit(tmp_path):
    _, fake, _, _ = llm_run(cooperator(), tmp_path, rounds=2, cap=every_grant(), observation_mode="push")
    assert {tuple(tools_offered(b)) for b in act_bodies(fake)} == {("calc_profit", "write_note", "read_note", "set_price", "hold")}


def test_with_no_queries_calc_profit_and_the_notes_are_still_offered(tmp_path):
    _, fake, _, _ = llm_run(cooperator(), tmp_path, rounds=2, cap=every_grant(queries=0))
    assert {tuple(tools_offered(b)) for b in act_bodies(fake)} == {("calc_profit", "write_note", "read_note", "set_price", "hold")}


def reader(body):
    """Talk call: read the channel once when it is offered, then answer. Act call: set the price."""
    offered = tools_offered(body)
    if "set_price" in offered:
        return call("set_price", price=JOINT)
    if "read_channel" in offered and not any(m["role"] == "tool" for m in body["messages"]):
        return call("read_channel", channel_id="private_A_B")
    return text("I hold 10.5.")


def test_in_pull_mode_a_talk_call_reads_the_channel_before_it_posts_also_under_c3_isolation(tmp_path):
    """CR-71 1: the pull observation holds no messages, so the talk call offers read_channel as a query. Under
    c3_isolation the act call still reads nothing, so the talk call is the only place the reply can be read."""
    caps = capability(channels=("private_A_B",), queries=2)
    talker = ("scripted_talker", {"price": JOINT, "offer_text": "B says hold 10.5", "channel_id": "private_A_B"}, caps)
    kw = dict(rounds=3, cap=caps, b=talker, observation_mode="pull", c3_isolation=True, call_mode="talk_and_act", private_pairs=True)
    sink, fake, _, _ = llm_run(reader, tmp_path, **kw)
    talk = [b for b in fake.bodies if "set_price" not in tools_offered(b) and not any(m["role"] == "tool" for m in b["messages"])]
    assert talk and all("read_channel" in tools_offered(b) for b in talk), "every pull talk call offers read_channel"
    assert all("read_channel" not in tools_offered(b) for b in act_bodies(fake)), "an isolated act call reads no channel"
    reads = of(sink, "tool_called", agent_id="A", tool="read_channel")
    assert len(reads) == 3 and all(e.allowed for e in reads)
    assert [m["text"] for m in reads[-1].result if m["from"] == "B"] == ["B says hold 10.5"] * 2, "B's replies of rounds 1-2"
    assert [e.text for e in of(sink, "posted", agent_id="A")] == ["I hold 10.5."] * 3


def test_in_push_mode_a_talk_call_offers_no_read(tmp_path):
    caps = capability(channels=("private_A_B",), queries=2)
    talker = ("scripted_talker", {"price": JOINT, "offer_text": "B says hold 10.5", "channel_id": "private_A_B"}, caps)
    _, fake, _, _ = llm_run(reader, tmp_path, rounds=2, cap=caps, b=talker, observation_mode="push", private_pairs=True)
    assert not any("read_channel" in tools_offered(b) for b in fake.bodies)


# §3.3 the tools


def tool_user(body):
    """Each act call: calc_profit, get_price_history, write_note, read_note, then set_price."""
    if "set_price" not in tools_offered(body):
        return text("PASS")
    t = int(re.match(r"Round (\d+)\.", user_text(body)).group(1))
    plan = [
        call("calc_profit", price=7.0, rival_prices={"B": JOINT}),
        call("get_price_history", rounds=6),
        call("write_note", text=f"note {t}"),
        call("read_note"),
    ]
    done = sum(m["role"] == "tool" for m in body["messages"])
    return plan[done] if done < len(plan) else call("set_price", price=JOINT)


def test_an_llm_seller_uses_each_new_tool_and_the_notes_persist(tmp_path):
    sink, _, _, _ = llm_run(tool_user, tmp_path, rounds=8, cap=every_grant(), policy_kw={"max_steps": 6})
    results = {(e.t, e.tool): e for e in of(sink, "tool_called", agent_id="A")}
    assert all(e.allowed for e in results.values())
    # q = a - b p + d mean(rivals) = 10 - 1.5 * 7 + 10.5 = 10; profit = (7 - 1) * 10
    assert results[(1, "calc_profit")].result == {
        "price": 7.0,
        "rival_prices": {"B": JOINT},
        "quantity": 10.0,
        "profit": 60.0,
        "basis": "base demand without shocks",
    }
    history = results[(8, "get_price_history")].result
    assert [r["t"] for r in history["B"]] == [2, 3, 4, 5, 6, 7], "rounds beyond the get_rival_prices depth 5"
    assert [r["t"] for r in history["A"]] == [2, 3, 4, 5, 6, 7]
    notes = [{"t": t, "text": f"note {t}"} for t in range(1, 9)]
    assert results[(8, "read_note")].result_hash == result_hash(notes), "read_note returns every earlier note"
    assert [(e.t, e.plan) for e in of(sink, "planned", agent_id="A", source="note")] == [(n["t"], n["text"]) for n in notes]
    tier_a = [e for e in sink.events if TIER[e.type] == "A"]
    assert not [e for e in tier_a if "note " in e.model_dump_json()], "the note text is tier C only"
    assert all(results[(t, n)].arguments == {} and results[(t, n)].result is None for t in range(1, 9) for n in ("write_note", "read_note"))


def test_calc_profit_without_a_market_is_an_error_result():
    v = view("both")
    assert v.profit_at("A", 2, 7.0, {"B": 7.0}, rich_state()) == {"error": "calc_profit is not available in this run"}


def test_calc_profit_uses_the_base_demand_and_by_default_the_rival_prices_the_agent_saw():
    shock = Event(announce=1, effect=1, length=30, shocks=[Shock(path="/c", op="add", value=2.0)], visible=["A", "B"], news="Costs rose.")
    scenario = frozen_scenario(events=[shock])
    blind = every_grant().model_copy(update={"grants": [g for g in every_grant().grants if g.tool_id != "get_rival_prices"]})
    caps = {"A": every_grant(), "B": blind}
    sp = spec(caps, {"A": "constant", "B": "constant"})
    market = scenario.market.build()
    policies = {
        s: make_scripted("constant", PolicyContext(seller_id=s, sellers=("A", "B"), market=market, params=scenario.params), price=JOINT)
        for s in "AB"
    }
    run = Run(sp, scenario, policies, Recorder(sp.run_id, [MemorySink()], now=lambda: "x"))
    state = rich_state()
    seen = run.view.profit_at("A", 9, 7.0, None, state)
    # q = 10 - 1.5 * 7 + 7; c = 1 as in the base, not 3 as in the shocked round
    assert (seen["rival_prices"], seen["quantity"], seen["profit"]) == ({"B": 7.0}, 6.5, 39.0)
    assert run.view.profit_at("B", 9, 7.0, None, state) == {"error": "give rival_prices for exactly these sellers: A"}
    assert run.view.profit_at("B", 9, 7.0, {"A": 6.0}, state)["quantity"] == 10 - 1.5 * 7 + 6


def test_under_c3_isolation_an_act_call_reads_only_notes_written_in_act_calls():
    v, state = view("both", c3=True), RunState()
    for t, purpose in ((1, "talk"), (1, "act"), (2, "talk")):
        box = v.toolbox("A", t, state, purpose, QueryBudget(3), lambda *a: None)
        assert box.call("write_note", text=f"{purpose} {t}") == {"saved": True, "t": t}

    def read(agent, purpose, s=state):
        return [n["text"] for n in v.toolbox(agent, 3, s, purpose, QueryBudget(3), lambda *a: None).call("read_note")]

    assert read("A", "act") == ["act 1"]
    assert read("A", "talk") == ["talk 1", "act 1", "talk 2"]
    assert read("B", "talk") == []
    assert read("A", "talk", RunState.restore(state.snapshot()[1])) == ["talk 1", "act 1", "talk 2"], "notes are in the snapshot"
    open_view = view("both", c3=False)
    assert [n["text"] for n in open_view.toolbox("A", 3, state, "act", QueryBudget(3), lambda *a: None).call("read_note")] == [
        "talk 1",
        "act 1",
        "talk 2",
    ]


def test_every_tool_module_imports_only_the_tool_registry_and_models():
    """DECISIONS-15 §2.3: a tool is a pure function on the run state; no tool module imports I/O."""
    src = Path(__file__).parents[1] / "src" / "simcore"
    allowed = {
        "__future__",
        "dataclasses",
        "typing",
        "pydantic",
        "simcore.tools",
        "simschema.base",
        "simschema.events",
        "simschema.plugins",
    }
    for path in [src / "tools.py", *(src / "builtin" / "tools").glob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert names <= allowed, (path.name, names - allowed)


# §3.4 the freeze check


def edit_condition(base: Path, cid: str, change) -> None:
    path = base / "conditions" / f"{cid}.yaml"
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")


@pytest.mark.parametrize(
    "cid, change, where",
    [
        (
            "C2a",
            lambda t: t.replace("      - {tool_id: hold}\n", "      - {tool_id: hold}\n      - {tool_id: calc_profit}\n", 1),
            "C2a/sellers/A/grants",
        ),
        (
            "C2b",
            lambda t: t.replace("{tool_id: get_rival_prices, params: {depth: 1}}", "{tool_id: get_rival_prices, params: {depth: 3}}"),
            "C2b/sellers/A/grants",
        ),
        ("C3-silent", lambda t: t.replace("observation_mode: push", "observation_mode: both"), "C3-silent/observation_mode"),
        ("C1", lambda t: t.replace("observation_mode: push", "observation_mode: both"), "C2a/observation_mode"),
    ],
)
def test_the_freeze_refuses_conditions_that_differ_outside_the_channels(client, root, cid, change, where):  # noqa: F811
    rubric_file(root)
    edit_condition(root, cid, change)
    r = freeze(client, "conditions")
    assert r.status_code == 409 and r.json()["code"] == "freeze_incomplete", r.text
    paths = [e["path"] for e in r.json()["errors"]]
    assert where in paths, paths


def test_the_channel_grants_c3_isolation_and_prompts_may_differ_at_the_freeze(client, root):  # noqa: F811  # noqa: F811
    rubric_file(root)
    r = freeze(client, "conditions")
    assert r.status_code == 200, r.text


def test_the_freeze_check_compares_each_sellers_query_limit(research):  # noqa: F811
    """CR-71 m1: Capability.max_queries_per_round is apart from the limits; a condition file cannot set it today, so the
    difference is made on the resolved condition."""
    c1, c2a = research.condition("C1"), research.condition("C2a")
    assert not [p for p in setting_problems([c1, c2a]) if "max_queries_per_round" in p["path"]]
    caps = {s: c.model_copy(update={"max_queries_per_round": 1}) for s, c in c2a.capabilities.items()}
    paths = [p["path"] for p in setting_problems([c1, c2a.model_copy(update={"capabilities": caps})])]
    assert {"C2a/sellers/A/max_queries_per_round", "C2a/sellers/B/max_queries_per_round"} <= set(paths), paths
