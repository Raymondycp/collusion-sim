"""Builders for known-answer tests: a frozen scenario, a run spec and scripted policies."""

from __future__ import annotations

from simcore.agents import PolicyContext
from simcore.recorder import MemorySink, Recorder
from simcore.run import Run, RunSpec
from simcore.scenario import DemandRef, Horizon, MarketSpec, Scenario
from simcore.scripted import make_scripted
from simschema.events import AgentHeader, Capability, Labels, ToolGrant

BASE = {"a": 10, "b": 1.5, "d": 1.0, "c": 1.0}
NASH, JOINT, BR_TO_JOINT = 5.75, 10.5, 7.35


def frozen_scenario(events=(), rounds=30, n=2):
    return Scenario(
        id="scn_known",
        name="known answers",
        market=MarketSpec(demand=DemandRef(id="linear", params=BASE)),
        n_sellers=n,
        horizon=Horizon(min_rounds=rounds, max_rounds=rounds, stop_prob=0),
        events=list(events),
        **({} if rounds > 25 else {"quiet_window": (0, 0)}),
    ).frozen(approved_by="test")


def capability(channels=(), see_rivals=True, queries=3, post=None):
    grants = [ToolGrant(tool_id="get_news"), ToolGrant(tool_id="get_own_sales"), ToolGrant(tool_id="set_price"), ToolGrant(tool_id="hold")]
    if see_rivals:
        grants.append(ToolGrant(tool_id="get_rival_prices", params={"depth": 5}))
    if channels:
        grants.append(ToolGrant(tool_id="read_channel", params={"channels": list(channels)}))
    post_channels = list(channels) if post is None else list(post)
    if post_channels:
        grants.append(ToolGrant(tool_id="post", params={"channels": post_channels}))
    return Capability(grants=grants, max_queries_per_round=queries)


def spec(caps: dict[str, Capability], kinds: dict[str, str], seed=1, **kw):
    agents = [AgentHeader(id=s, role="seller", policy_kind=kinds[s], capability=caps[s]) for s in sorted(caps)]
    agents += kw.pop("extra_agents", [])
    base = dict(run_id="run_TEST", kind="exploratory", split="exploratory", seed=seed, agents=agents, created_at="2026-10-21T00:00:00Z")
    if kw.pop("research", False):
        base.update(kind="research", split="dev", condition_id="C1", labels=Labels(legal="yes", harm="yes"))
    return RunSpec(**{**base, **kw})


def run(policies: dict[str, tuple[str, dict]], caps=None, scenario=None, seed=1, run_id="run_TEST", sinks=None, **kw):
    scenario = scenario or frozen_scenario()
    caps = caps or {s: capability() for s in policies}
    sp = spec(caps, {s: k for s, (k, _) in policies.items()}, seed=seed, run_id=run_id, **kw)
    market = scenario.market.build()
    built = {
        s: make_scripted(k, PolicyContext(seller_id=s, sellers=tuple(sorted(policies)), market=market, params=scenario.params), **p)
        for s, (k, p) in policies.items()
    }
    sinks = sinks or [MemorySink()]
    sink = sinks[0]
    recorder = Recorder(sp.run_id, sinks, now=lambda: "2026-10-21T00:00:00.000Z")
    state = Run(sp, scenario, built, recorder).execute()
    return sink, state, sp, scenario, built


def of(sink, kind, **match):
    return [e for e in sink.events if e.type == kind and all(getattr(e, k) == v for k, v in match.items())]


def prices(sink, seller):
    return {e.t: e.prices[seller] for e in of(sink, "settled")}


def punishment_depth(sink, seller, window):
    """D = Σ max(0, BR − p) over the window (plan §09), with BR to the rival's last price."""
    settled = {int(e.t): e for e in of(sink, "settled")}
    refs = {int(e.t): e for e in of(sink, "reference")}
    return sum(max(0.0, refs[t].best_response[seller] - settled[t].prices[seller]) for t in window)
