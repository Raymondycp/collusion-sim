"""User requirement (22 Oct; DECISIONS-15 §3.1): private channel, public channel, news and every tool are on or off per run
and per agent.

Two LLM sellers on the fake endpoint, talk_and_act. The fake brain ignores what it is shown, so prices and posts are the
same in every run. Each switch is compared with an all-on baseline: the pushed observation, the offered tools and the
request text the policy built change in that item only, for the agent that lost it (the other agent can lose only the
messages the first one no longer posts). Query limit 0 checks the push; query limit 1 checks the read tools. C3
isolation is checked by content, off and on. No paid call (CR-69)."""

import json
import re
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from adapters.llm.policy import LLMPolicy
from api.app import create_app
from fakellm import FakeLLM, call, text, tools_offered
from services import configs
from services.configs import RunConfig
from services.research import ConditionFile
from simcore.run import RunSpec
from test_d13 import run_condition
from test_profiles import MemoryKeyring, profile_body
from test_research import ROOT, research_config, started
from condcopy import draft_conditions

PRIVATE, PUBLIC = "private_A_B", "public"
MODELS = {"A": "m-small", "B": "m-large"}  # the request body's model tells the agent
PRICES = {"m-small": 10.5, "m-large": 9.5}
READS = ("get_news", "get_rival_prices", "get_own_sales", "read_channel")
MESSAGES = {"new_messages", "recent_messages", "new_message_count"}
NEWS = "Marginal cost will"  # the start of both news items of scn_s1
# item: (grants dropped, channel dropped from read_channel and post, observation fields that may change, request-text marker)
ITEMS = {
    "private": ((), PRIVATE, MESSAGES, f"on {PRIVATE}"),
    "public": ((), PUBLIC, MESSAGES, f"on {PUBLIC}"),
    "news": (("get_news",), None, {"news", "headlines", "masked"}, NEWS),
    "rival_prices": (("get_rival_prices",), None, {"rival_last_prices", "rival_history"}, "Rival prices last round"),
    "own_sales": (("get_own_sales",), None, {"own", "own_history"}, "units sold"),
}


def brain(body: dict):
    """Act: a fixed price per agent. Talk with the post tool: one post per turn, on the channels in turn by round, so both
    carry text. Talk with one channel: a plain-text reply, posted there."""
    offered = tools_offered(body)
    if "set_price" in offered:
        return call("set_price", price=PRICES[body["model"]], plan="Keep the price.")
    if "post" in offered:
        post = next(t for t in body["tools"] if t["function"]["name"] == "post")
        channels = post["function"]["parameters"]["properties"]["channel_id"]["enum"]
        t = int(re.search(r"Round (\d+)\.", body["messages"][1]["content"]).group(1)) + (body["model"] == MODELS["B"])
        channel = channels[t % len(channels)]
        return call("post", channel_id=channel, text=f"{body['model']} on {channel}")
    return text(f"{body['model']} plain")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    seen: list[tuple[str, str, list[str], str]] = []
    first = LLMPolicy._first_request

    def record(self, obs, tools, purpose):  # each turn's offer and text, also when the call cache answers the endpoint
        request, reads = first(self, obs, tools, purpose)
        names = [t["function"]["name"] if "function" in t else t["name"] for t in request.tools]
        seen.append((self.seller_id, purpose, names, request.messages[1]["content"]))
        return request, reads

    monkeypatch.setattr(LLMPolicy, "_first_request", record)
    fake = FakeLLM(brain)
    with TestClient(create_app(tmp_path, use_dbos=False, key_backend=MemoryKeyring(), llm_transport=fake.transport)) as c:
        c.fake, c.seen = fake, seen
        c.pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
        c.scenario = c.get("/api/scenarios/scn_s1").json()
        c.schema = Draft202012Validator(c.get("/api/schema/run-config").json())
        yield c


@pytest.fixture(params=[False, True], ids=["c3_off", "c3_on"])
def isolation(request, monkeypatch):
    """C3 isolation comes only from a research condition, and a condition fixes the grants (and at query limit 0 needs
    get_news when it publishes news). So the free grant sets here reach isolation by building the exploratory RunSpec
    with c3_isolation=True; test_isolation_through_the_research_conditions covers the real route (CR-69 6)."""
    if request.param:
        monkeypatch.setattr(configs, "RunSpec", lambda **kw: RunSpec(**kw, c3_isolation=True))
    return request.param


def grants(drop=(), read=(PRIVATE, PUBLIC), post=(PRIVATE, PUBLIC)) -> list[dict]:
    out = [{"tool_id": t} for t in ("get_own_sales", "get_news", "set_price", "hold") if t not in drop]
    if "get_rival_prices" not in drop:
        out.append({"tool_id": "get_rival_prices", "params": {"depth": 5}})
    if read:
        out.append({"tool_id": "read_channel", "params": {"channels": list(read)}})
    if post:
        out.append({"tool_id": "post", "params": {"channels": list(post)}})
    return out


def config(c, queries=0, channels=None, transparency="rival_last_round", **by_agent) -> dict:
    """Every item on for both sellers; by_agent[s] gives seller s other grants."""
    agents = [
        {
            "id": s,
            "role": "seller",
            "policy": {
                "kind": "llm",
                "llm": {"provider_profile_id": c.pid, "model": MODELS[s], "model_family": f"fam-{s}", "temperature": 0},
            },
            "capability": {"grants": by_agent.get(s) or grants(), "max_queries_per_round": queries},
        }
        for s in ("A", "B")
    ]
    body = {
        "kind": "exploratory",
        "scenario_id": "scn_s1",
        "scenario_hash": c.scenario["hash"],
        "seeds": [1],
        "agents": agents,
        "channels": {"public": True, "private_pairs": True, "news": True, **(channels or {})},
        "transparency": transparency,
        "call_mode": "talk_and_act",
        "clock": {"mode": "sync"},
        "limits": {"max_queries_per_round": queries},
        "budget": {"max_calls": 2000},
    }
    assert not list(c.schema.iter_errors(body)), "the web/CLI form schema accepts the combination"
    return body


RUNS: dict[str, "Run"] = {}
"""Runs by config (and isolation), shared by the tests of this file: the fake brain is blind, so one config gives one
episode (CR-69 addendum 3). Right only while no test changes behaviour by other means: a test that patches the policy,
View or the brain must not use episode(), or must add the patch to the key, as the isolation patch is."""


def episode(c, body: dict) -> "Run":
    key = json.dumps(body, sort_keys=True).replace(c.pid, "PID") + f"|isolation={configs.RunSpec is not RunSpec}"
    if key not in RUNS:
        RUNS[key] = Run(c, body)
    return RUNS[key]


class Run:
    def __init__(self, c, body: dict) -> None:
        c.seen.clear()
        (rid,) = started(c, body)
        assert c.get(f"/api/runs/{rid}").json()["status"] == "completed"
        events = c.get(f"/api/runs/{rid}/events", params={"limit": 5000}).json()["events"]
        self.header = json.loads(c.get(f"/api/runs/{rid}/episode.jsonl").text.splitlines()[0])
        self.obs = {(e["agent_id"], e["t"], e["purpose"]): e["observation"] for e in events if e["type"] == "observed"}
        self.posts = {e["text"] for e in events if e["type"] == "posted"}
        self.tools: dict = {}
        self.texts: dict = {}
        for agent, purpose, names, user in c.seen:
            self.tools.setdefault((agent, purpose), []).append(names)
            self.texts.setdefault((agent, purpose), []).append(user)

    def of(self, agent: str, purpose: str | None = None) -> list[dict]:
        return [o for (s, _, p), o in self.obs.items() if s == agent and purpose in (None, p)]

    def text(self, agent: str, purpose: str | None = None) -> str:
        return "\n".join(x for (s, p), xs in self.texts.items() if s == agent and purpose in (None, p) for x in xs)


def channel_ids(o: dict) -> set[str]:
    return {m["channel_id"] for m in o["recent_messages"] + o["new_messages"]}


def changed(base: dict, other: dict) -> set[str]:
    return {k for k in base.keys() | other.keys() if base.get(k) != other.get(k)}


def check_isolation(run: Run, isolated: bool) -> None:
    """CR-69 2: by content, not by the masked label. Isolated act calls hold no message and their request no message text."""
    acts = [o for s in "AB" for o in run.of(s, "act")]
    if isolated:
        assert all(o["new_messages"] == [] and o["recent_messages"] == [] and o["new_message_count"] == 0 for o in acts)
        acting = run.text("A", "act") + run.text("B", "act")
        assert "Message" not in acting and "message (round" not in acting, "no message line in an isolated act request"
        assert run.posts and not [x for x in run.posts if x in acting], "no post text in an isolated act request"
        assert any(channel_ids(o) for s in "AB" for o in run.of(s, "talk")), "the talk calls of the same run hold messages"
    else:
        assert any(channel_ids(o) for o in acts), "without isolation the act call reads the channels"


@pytest.mark.slow
def test_each_item_switched_off_for_one_agent_changes_only_that_item(client, isolation):
    base = episode(client, config(client))
    check_isolation(base, isolation)
    for agent, other in (("A", "B"), ("B", "A")):
        for item, (drop, channel, may_change, marker) in ITEMS.items():
            keep = tuple(c for c in (PRIVATE, PUBLIC) if c != channel)
            run = episode(client, config(client, **{agent: grants(drop, read=keep, post=keep)}))
            check_isolation(run, isolation)
            assert run.obs.keys() == base.obs.keys(), (item, agent, "same calls: the item changes no turn")
            for key, o in run.obs.items():
                diff = changed(base.obs[key], o)
                if key[0] == agent:
                    assert diff <= may_change, (item, key, diff)
                elif channel:
                    assert diff <= MESSAGES, (item, key, diff, "the other agent loses only the messages the first one moved")
                else:
                    assert not diff, (item, key, diff)
            assert marker in base.text(agent) and marker not in run.text(agent), (item, agent, "the request text follows")
            assert channel or run.text(other) == base.text(other), (item, other, "the other agent's requests are unchanged")
            mine = run.of(agent)
            if item == "news":
                assert not any(o["news"] or o["headlines"] for o in mine) and all("news" in o["masked"] for o in mine)
            elif item == "rival_prices":
                assert not any(o["rival_last_prices"] or o["rival_history"] for o in mine)
            elif item == "own_sales":
                assert not any(o["own_history"] or o["own"]["last_price"] is not None for o in mine)
            else:
                assert channel not in set().union(*map(channel_ids, mine)) and channel in set().union(*map(channel_ids, base.of(agent)))
            for key, offered in run.tools.items():
                if key == (agent, "talk") and channel:
                    assert all(t == [] for t in offered) and all(t == ["post"] for t in base.tools[key]), "one channel: plain text"
                else:
                    assert offered == base.tools[key], (item, key)
    assert all(t == ["set_price", "hold"] for k, ts in base.tools.items() if k[1] == "act" for t in ts), "query limit 0: no read"


@pytest.mark.slow
def test_each_run_switch_wins_over_the_grants(client):
    """CR-69 1: the run flags off with every grant on. The item goes for both agents and from the recorded capability: news
    and transparency drop their grant; a closed channel leaves the header, read_channel and post, and with both channels
    closed the grants go and no talk turn is left (CR-69 addendum 2). A channel grant given no channel counts as no grant
    (CR-69 addendum 3)."""
    base = episode(client, config(client))
    for name, kw, gone in (
        ("public", {"channels": {"public": False}}, (PUBLIC,)),
        ("private_pairs", {"channels": {"private_pairs": False}}, (PRIVATE,)),
        ("both channels", {"channels": {"public": False, "private_pairs": False}}, (PUBLIC, PRIVATE)),
        ("news", {"channels": {"news": False}}, ()),
        ("transparency", {"transparency": "opaque"}, ()),
    ):
        r = episode(client, config(client, **kw))
        silent = len(gone) == 2
        assert r.obs.keys() == {k for k in base.obs if not (silent and k[2] == "talk")}, name
        grants_of = {a["id"]: {g["tool_id"]: g["params"] for g in a["capability"]["grants"]} for a in r.header["agents"]}
        caps = {s: set(g) for s, g in grants_of.items()}
        for s in "AB":
            mine = r.of(s)
            if silent:
                assert r.header["channels"] == [] and not caps[s] & {"read_channel", "post"}, (name, s)
                assert (s, "talk") not in r.tools and not r.posts and not any(channel_ids(o) for o in mine), (name, s)
            elif gone:
                (closed,) = gone
                left = [c for c in (PRIVATE, PUBLIC) if c != closed]
                assert closed not in {c["id"] for c in r.header["channels"]} and closed not in set().union(*map(channel_ids, mine))
                assert grants_of[s]["read_channel"]["channels"] == grants_of[s]["post"]["channels"] == left, (name, s)
                assert f"on {closed}" not in r.text(s) and all(t == [] for t in r.tools[(s, "talk")]), (name, s)
            elif name == "news":
                assert "get_news" not in caps[s] and not any(o["news"] or o["headlines"] for o in mine) and NEWS not in r.text(s)
            else:
                assert "get_rival_prices" not in caps[s] and not any(o["rival_last_prices"] for o in mine)
                assert "Rival prices last round" not in r.text(s)
    assert all(NEWS in base.text(s) and "Rival prices last round" in base.text(s) for s in "AB")
    empty = [*grants(read=(), post=()), {"tool_id": "post", "params": {"channels": []}}, {"tool_id": "read_channel"}]
    r = episode(client, config(client, A=empty))
    assert not {g["tool_id"] for g in r.header["agents"][0]["capability"]["grants"]} & {"post", "read_channel"}
    assert ("A", "talk") not in r.tools and not r.of("A", "talk") and r.of("B", "talk"), "no channel named: no talk turn"


@pytest.mark.slow
def test_with_a_query_budget_each_read_tool_is_offered_exactly_when_granted(client):
    """CR-69 3: query limit 1. A read tool is offered in the act call only with its grant; the push follows the same grant."""
    base = episode(client, config(client, queries=1))
    assert all(set(READS) <= set(t) for t in base.tools[("A", "act")]), base.tools[("A", "act")]
    assert all(t == ["post"] for t in base.tools[("A", "talk")]), "a talk call offers no read tool"
    push = {"get_news": "news", "get_rival_prices": "rival_last_prices", "get_own_sales": "own_history", "read_channel": "recent_messages"}
    for tool in READS:
        read = () if tool == "read_channel" else (PRIVATE, PUBLIC)
        run = episode(client, config(client, queries=1, A=grants(drop=(tool,), read=read)))
        assert run.tools[("A", "act")] == [[n for n in t if n != tool] for t in base.tools[("A", "act")]], tool
        assert run.tools[("B", "act")] == base.tools[("B", "act")], tool
        assert not any(o[push[tool]] for o in run.of("A")) and any(o[push[tool]] for o in base.of("A")), tool


def test_read_and_post_are_switched_apart(client, isolation):
    """CR-69 5: a post grant with no read grant posts and reads nothing; a read grant with no post grant has no talk turn
    and reads only in the act call, so nothing at all under isolation."""
    post_only = episode(client, config(client, A=grants(read=())))
    assert post_only.of("A", "talk") and not any(channel_ids(o) for o in post_only.of("A"))
    assert "m-small on" in post_only.text("B") and "m-large on" not in post_only.text("A"), "B reads A; A reads no one"
    read_only = episode(client, config(client, A=grants(post=())))
    assert not read_only.of("A", "talk") and ("A", "talk") not in read_only.tools, "no post grant: no talk turn"
    heard = any(channel_ids(o) for o in read_only.of("A", "act"))
    assert heard is not isolation and ("m-large on" in read_only.text("A")) is not isolation


@pytest.mark.parametrize("cid", ["C3-accept", "C3-silent"])
def test_isolation_through_the_research_conditions(client, cid):
    """CR-69 6: the real route. Every act call of a C3 condition holds no message, in the observation and the request."""
    client.seen.clear()
    events, cond = run_condition(client, cid)
    assert cond["c3_isolation"]
    acts = [e["observation"] for e in events if e["type"] == "observed" and e["purpose"] == "act"]
    assert acts and all(o["new_messages"] == [] and o["recent_messages"] == [] and "channels" in o["masked"] for o in acts)
    posts = {e["text"] for e in events if e["type"] == "posted"}
    acting = [x for _, p, _, x in client.seen if p == "act"]
    assert posts and not [x for x in acting if "Message" in x or "message (round" in x or any(m in x for m in posts)]
    to_b = [m for e in events if e["type"] == "observed" and e["agent_id"] == "B" for m in e["observation"]["recent_messages"]]
    assert any(m["sender"] == "A" for m in to_b) is (cid == "C3-accept"), "B reads A's offer in its talk call in C3-accept only"


SAME = {s: ("constant", {"price": 10.5}) for s in "AB"}  # for a condition that needs the same model for both sellers


@pytest.mark.parametrize("cid", sorted(p.stem for p in (ROOT / "conditions").glob("C*.yaml")))
def test_a_research_run_records_exactly_the_conditions_capabilities(client, cid):
    """CR-69 addendum 1: the run switches take nothing from a research condition, so its run header holds the condition's
    capabilities. It fails the day a condition grants what its own switches take away."""
    research = client.app.state.research
    cond = client.get(f"/api/conditions/{cid}").json()
    (seed_run,) = research.expand(
        RunConfig.model_validate(research_config(cid, seeds=(1,), policies=SAME if cond["same_model_for_all_sellers"] else None))
    )
    (spec,) = configs.build_specs(
        seed_run.config, seed_run.scenario, "batch_X", ["run_X"], "2026-10-22T00:00:00Z", research=seed_run.binding
    )
    assert {a.id: a.capability.model_dump(mode="json") for a in spec.agents if a.role == "seller"} == cond["capabilities"]


def test_a_condition_file_grants_nothing_that_its_switches_take_away():
    c1 = yaml.safe_load((ROOT / "conditions" / "C1.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="news is off, so the condition grants no get_news"):
        ConditionFile.model_validate({**c1, "channels": {**c1["channels"], "news": False}})
    with pytest.raises(ValidationError, match=r"read_channel names \['private_A_B'\], which the condition does not open"):
        ConditionFile.model_validate({**c1, "channels": {**c1["channels"], "private_pairs": False, "public": True}})

    def with_post(channels):
        a = {**c1["sellers"]["A"], "grants": [g for g in c1["sellers"]["A"]["grants"] if g["tool_id"] != "post"]}
        a["grants"].append({"tool_id": "post", "params": {"channels": channels}})
        return {**c1, "sellers": {**c1["sellers"], "A": a}}

    with pytest.raises(ValidationError, match="post names no channel"):
        ConditionFile.model_validate(with_post([]))
    with pytest.raises(ValidationError, match=r"post names \['private_A_C'\], which is not public or a private pair of A"):
        ConditionFile.model_validate(with_post(["private_A_C"]))
    ConditionFile.model_validate(with_post(["private_A_B"]))
