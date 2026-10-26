import pytest

from simcore.agents import CapabilityError
from simcore.roles import ROLES
from simcore.state import PricePoint, RunState
from simcore.tools import TOOLS, GrantError
from simcore.view import QueryBudget, View
from simkit import JOINT, NASH, capability, frozen_scenario, of, run
from simschema.events import AgentHeader, Capability, ChannelInfo, ToolGrant


def caps(*grants, queries=3):
    return Capability(grants=[ToolGrant(tool_id=t, params=p) for t, p in grants], max_queries_per_round=queries)


def test_registry_fills_default_grant_params():
    c = TOOLS.normalise(caps(("get_rival_prices", {})), "seller", ROLES["seller"].tools)
    assert c.grant("get_rival_prices").params == {"depth": 5}


@pytest.mark.parametrize(
    "grant, role",
    [
        (("teleport", {}), "seller"),
        (("post", {"channels": ["public"]}), "host"),
        (("get_rival_prices", {"depth": 99}), "seller"),
        (("get_rival_prices", {"colour": "red"}), "seller"),
    ],
)
def test_registry_rejects_bad_grants(grant, role):
    with pytest.raises(GrantError):
        TOOLS.normalise(caps(grant), role, ROLES[role].tools)


def test_registry_rejects_a_tool_granted_twice():
    with pytest.raises(GrantError):
        TOOLS.normalise(caps(("get_news", {}), ("get_news", {})), "seller", ROLES["seller"].tools)


def test_every_tool_publishes_json_schemas_for_its_grant_and_its_call():
    for spec in TOOLS.specs():
        assert spec.params_schema["type"] == "object" and spec.input_schema["type"] == "object"
    queries = {s.id for s in TOOLS.specs() if s.counts_as_query}  # reads of the record (DECISIONS-15 §3.3)
    assert queries == {"get_rival_prices", "get_news", "read_channel", "get_own_sales", "get_price_history"}


def _view(capability, c3=False):
    channels = [ChannelInfo(id="private_A_B", kind="private", participants=["A", "B"])]
    cap = TOOLS.normalise(capability, "seller", ROLES["seller"].tools)
    return View(channels, {"A": cap, "B": cap}, c3)


def _state():
    return RunState(
        history={"A": [PricePoint(t=t, price=6.0) for t in range(1, 9)], "B": [PricePoint(t=t, price=7.0) for t in range(1, 9)]}
    )


def test_grant_depth_caps_what_the_rival_price_tool_returns():
    view = _view(caps(("get_rival_prices", {"depth": 2})))
    calls = []
    box = view.toolbox("A", 9, _state(), "act", QueryBudget(3), lambda *a: calls.append(a))
    out = box.call("get_rival_prices", rounds=10)
    assert [p["t"] for p in out["B"]] == [7, 8]


def test_a_read_tool_never_returns_what_view_hides():
    view = _view(caps(("read_channel", {"channels": ["private_A_B"]}), ("post", {"channels": ["private_A_B"]})), c3=True)
    state = _state()
    for purpose, available in (("talk", True), ("act", False)):
        box = view.toolbox("B", 9, state, purpose, QueryBudget(3), lambda *a: None)
        assert box.has("read_channel") is available
    box = view.toolbox("B", 9, state, "talk", QueryBudget(3), lambda *a: None)
    assert box.call("read_channel", channel_id="public") is None


def test_ungranted_tools_are_not_in_the_toolbox_and_calls_to_them_are_refused():
    view = _view(caps(("get_news", {})))
    calls = []
    box = view.toolbox("A", 9, _state(), "act", QueryBudget(3), lambda *a: calls.append(a))
    assert [s.id for s in box.specs] == ["get_news"]
    assert box.call("get_rival_prices", rounds=1) is None
    assert calls[-1][:4] == ("get_rival_prices", "1", {"rounds": 1}, False)


def test_invalid_call_arguments_are_refused_and_recorded():
    view = _view(caps(("get_rival_prices", {})))
    calls = []
    box = view.toolbox("A", 9, _state(), "act", QueryBudget(3), lambda *a: calls.append(a))
    assert box.call("get_rival_prices", rounds="many") is None
    assert calls[-1][3] is False


def test_hold_without_a_hold_grant_is_refused():
    c = capability()
    no_hold = c.model_copy(update={"grants": [g for g in c.grants if g.tool_id != "hold"]})
    from simcore.agents import Action
    from simcore.scripted import Scripted

    class Holder(Scripted):
        kind = "holder"

        def act(self, obs, tools):
            return Action(hold=True)

    import simcore.scripted as scripted

    scripted.SCRIPTED["holder"] = Holder
    try:
        with pytest.raises(CapabilityError):
            run({"A": ("holder", {}), "B": ("constant", {"price": NASH})}, caps={"A": no_hold, "B": capability()})
    finally:
        del scripted.SCRIPTED["holder"]


@pytest.mark.parametrize(
    "agent",
    [
        AgentHeader(id="X", role="buyer", policy_kind="scripted_buyer", capability=Capability()),
        AgentHeader(id="J", role="judge", policy_kind="scripted_judge", capability=Capability()),
        AgentHeader(id="H", role="host", policy_kind="llm_host", capability=Capability()),
        AgentHeader(id="Q", role="pilot", policy_kind="x", capability=Capability()),
    ],
)
def test_roles_that_are_not_built_or_not_run_stage_cannot_join_a_run(agent):
    with pytest.raises(ValueError):
        run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}, extra_agents=[agent])


def test_a_scripted_host_may_join_a_research_run():
    host = AgentHeader(id="host", role="host", policy_kind="scripted_host", capability=Capability())
    sink, *_ = run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}, research=True, extra_agents=[host])
    header = of(sink, "run_started")[0].header
    assert [a.role for a in header.agents] == ["seller", "seller", "host"] and header.reproducible


def test_host_news_reaches_agents_only_through_the_state():
    import simkit
    from simcore.scenario import Event
    from simcore.shocks import Shock

    class LoudHost:
        kind = "test_host"

        def news_text(self, t, i, e):
            return e.news.upper()

    e = Event(announce=3, effect=3, length=2, shocks=[Shock(path="/c", op="add", value=0.5)], visible=["A", "B"], news="Oil prices rose.")
    original = simkit.Run

    class HostedRun(original):
        def __init__(self, *a, **kw):
            super().__init__(*a, host=LoudHost(), **kw)

    simkit.Run = HostedRun
    try:
        sink, *_ = run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": JOINT})}, scenario=frozen_scenario([e]))
    finally:
        simkit.Run = original
    assert [x.text for x in of(sink, "news_published")] == ["OIL PRICES ROSE."]
    assert {x.observation.headlines[0] for x in of(sink, "observed") if x.t == 3} == {"OIL PRICES ROSE."}
    assert of(sink, "run_started")[0].header.reproducible is False


def _with_dummy_role(monkeypatch, output_events, phases=("pull",)):
    from simcore.roles import RoleSpec

    role = RoleSpec(
        name="dummy", stage="run", phases=list(phases), tools=[], output_events=output_events, research_allowed=False, built=True
    )
    monkeypatch.setitem(ROLES._roles, "dummy", role)
    return AgentHeader(id="D", role="dummy", policy_kind="dummy", capability=Capability())


def _run_with(agent_header, phase_agent):
    import simkit
    from simcore.agents import PolicyContext
    from simcore.recorder import MemorySink, Recorder
    from simcore.run import Run
    from simcore.scripted import make_scripted

    scenario = frozen_scenario(rounds=3)
    sp = simkit.spec({"A": capability(), "B": capability()}, {"A": "constant", "B": "constant"}, extra_agents=[agent_header])
    market = scenario.market.build()
    sellers = {
        s: make_scripted("constant", PolicyContext(seller_id=s, sellers=("A", "B"), market=market, params=scenario.params), price=NASH)
        for s in ("A", "B")
    }
    sink = MemorySink()
    rec = Recorder(sp.run_id, [sink], now=lambda: "2026-10-21T00:00:00.000Z")
    Run(sp, scenario, sellers, rec, agents={"D": phase_agent(rec)}).execute()
    return sink


def test_a_role_registered_outside_run_py_joins_its_phase(monkeypatch):
    from simschema.events import OutcomeEvent

    header = _with_dummy_role(monkeypatch, ["outcome"])
    calls = []

    class Dummy:
        def __init__(self, rec):
            self.rec = rec

        def run_phase(self, phase, ctx):
            calls.append((ctx.t, phase, dict(ctx.prices)))
            self.rec.emit(OutcomeEvent, ctx.t, agent_id="D", values={"seen": 1.0})

    sink = _run_with(header, Dummy)
    assert calls == [(1, "pull", {}), (2, "pull", {}), (3, "pull", {})], "pull runs before act, so no price is set yet"
    assert [e.t for e in of(sink, "outcome")] == [1, 2, 3]
    order = [e.type for e in sink.events if e.t == 2]
    assert order.index("news_published" if "news_published" in order else "round_started") < order.index("outcome") < order.index("acted")


def test_a_non_seller_role_in_act_runs_after_every_seller_has_acted(monkeypatch):
    from simschema.events import OutcomeEvent

    header = _with_dummy_role(monkeypatch, ["outcome"], phases=("act",))
    seen = []

    class Buyerish:
        def __init__(self, rec):
            self.rec = rec

        def run_phase(self, phase, ctx):
            seen.append((ctx.t, dict(ctx.prices)))
            self.rec.emit(OutcomeEvent, ctx.t, agent_id="D", values={"seen": 1.0})

    sink = _run_with(header, Buyerish)
    assert seen == [(t, {"A": NASH, "B": NASH}) for t in (1, 2, 3)], "both sellers' prices are set when the buyer acts"
    for t in (1, 2, 3):
        order = [e.type for e in sink.events if e.t == t and e.type in ("acted", "outcome", "settled")]
        assert order == ["acted", "acted", "outcome", "settled"]


def test_a_role_may_record_only_its_output_events(monkeypatch):
    from simschema.events import PostedEvent

    header = _with_dummy_role(monkeypatch, ["outcome"])

    class Liar:
        def __init__(self, rec):
            self.rec = rec

        def run_phase(self, phase, ctx):
            self.rec.emit(PostedEvent, ctx.t, agent_id="D", channel_id="public", text="hi", deliver_t=ctx.t + 1)

    with pytest.raises(RuntimeError, match="not an output of role dummy"):
        _run_with(header, Liar)


def test_private_channel_ids_do_not_depend_on_the_agent_order_in_the_config():
    import simkit

    caps = {s: capability(channels=("private_A_B",)) for s in "AB"}
    forward = simkit.spec(caps, {"A": "constant", "B": "constant"}, private_pairs=True)
    backward = forward.model_copy(update={"agents": list(reversed(forward.agents))})
    assert [c.id for c in backward.channels()] == [c.id for c in forward.channels()] == ["private_A_B"]
    assert backward.channels()[0].participants == ["A", "B"]


@pytest.mark.parametrize("tool_id", [s.id for s in TOOLS.specs() if s.counts_as_query])  # the reads of the record
def test_every_read_tool_sees_only_what_the_agent_could_see_at_round_t(tool_id):
    from simcore.state import Message, NewsItem, Outcome
    from simcore.view import AgentView

    t = 5
    state = RunState()
    for r in range(1, 9):
        for s in "AB":
            state.history.setdefault(s, []).append(PricePoint(t=r, price=float(r)))
            state.outcomes.setdefault(s, []).append(Outcome(t=r, quantity=1.0, profit=1.0))
        state.messages.append(Message(id=r, t=r, channel_id="private_A_B", sender="B", text=f"m{r}", deliver_t=r + 1))
        state.news.append(NewsItem(t=r, event_index=r, text=f"n{r}", visible=["A"]))
    grants = [
        (g, {"channels": ["private_A_B"]} if g == "read_channel" else {})
        for g in ("get_rival_prices", "get_news", "read_channel", "get_own_sales", "get_price_history")
    ]
    view = View([ChannelInfo(id="private_A_B", kind="private", participants=["A", "B"])], {"A": caps(*grants), "B": caps()}, False)
    box = view.toolbox("A", t, state, "act", QueryBudget(3), lambda *a: None)
    args = {"read_channel": {"channel_id": "private_A_B"}}.get(
        tool_id, {"rounds": 10} if "rounds" in TOOLS[tool_id].input_model.model_fields else {}
    )
    result = box.call(tool_id, **args)
    rows = [r for v in result.values() for r in v] if isinstance(result, dict) else result
    assert rows, f"{tool_id} returned nothing to check"
    for row in rows:
        if "deliver_t" in row:
            assert row["deliver_t"] <= t
        elif tool_id in ("get_rival_prices", "get_own_sales", "get_price_history"):
            assert row["t"] < t, f"{tool_id} shows round {row['t']} at round {t}"
        else:
            assert row["t"] <= t
    assert not any(isinstance(v, RunState) for k, v in vars(box._view).items() if not k.startswith("_AgentView__"))
    assert isinstance(box._view, AgentView)
