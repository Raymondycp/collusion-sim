import hashlib
import json

import httpx2
import pytest

from adapters.llm import BudgetGuard, CallCache, Endpoint, LLMPolicy, ProviderProfile, request_seed
from adapters.llm.policy import NUDGE
from fakellm import FakeLLM, call, cooperator, text, tools_offered
from simcore.agents import PolicyContext
from simcore.recorder import MemorySink, Recorder
from simcore.run import Run
from simcore.scripted import make_scripted
from simcore.state import RunState
from simcore.view import QueryBudget, View
from simkit import JOINT, NASH, capability, frozen_scenario, of, prices, spec
from simschema.events import AgentHeader

KEY = "sk-fake-DO-NOT-LEAK-12345"
SYSTEM = "You run a petrol station in a small town. Each round you set one price. Maximise your total profit."
PROFILE = ProviderProfile(
    id="prof_fake",
    base_url="http://fake.local/v1",
    model="fake-model",
    family="fake",
    input_usd_per_mtok=1.0,
    output_usd_per_mtok=2.0,
    supports_seed=True,
)


def llm_run(
    brain,
    tmp_path,
    rounds=6,
    cache=None,
    budget=None,
    cap=None,
    profile=PROFILE,
    memory_every=5,
    transport=None,
    policy_kw=None,
    b=None,
    sink=None,
    **kw,
):
    """`transport`: another fake wire instead of FakeLLM(brain). `policy_kw`: more LLMPolicy arguments. `b`: seller B as
    (scripted kind, params, capability); default a constant seller at JOINT. `sink`: keeps the events when the run raises."""
    b_kind, b_params, b_cap = b or ("constant", {"price": JOINT}, capability())
    fake = FakeLLM(brain)
    scenario = frozen_scenario(rounds=rounds)
    cap = cap or capability()
    header = AgentHeader(
        id="A",
        role="seller",
        policy_kind="llm",
        capability=cap,
        provider_profile_id=profile.id,
        base_url_host="fake.local",
        model=profile.model,
        temperature=0.7,
    )
    sp = spec({"A": cap, "B": b_cap}, {"A": "llm", "B": b_kind}, **kw)
    sp = sp.model_copy(update={"agents": [header, *[a for a in sp.agents if a.id != "A"]]})
    sink = sink or MemorySink()
    rec = Recorder(sp.run_id, [sink], now=lambda: "2026-10-21T00:00:00.000Z")
    cache = cache or CallCache(tmp_path / "calls.sqlite")
    policy = LLMPolicy(
        seller_id="A",
        endpoint=Endpoint(profile, api_key=KEY, transport=transport or fake.transport()),
        system_prompt=SYSTEM,
        capability=sp.agents[0].capability,
        recorder=rec,
        cache=cache,
        budget=budget or BudgetGuard(),
        temperature=0.7,
        run_seed=sp.seed,
        memory_summary=sp.memory_summary,
        memory_every=memory_every,
        **(policy_kw or {}),
    )
    market = scenario.market.build()
    b = make_scripted(b_kind, PolicyContext(seller_id="B", sellers=("A", "B"), market=market, params=scenario.params), **b_params)
    Run(sp, scenario, {"A": policy, "B": b}, rec).execute()
    return sink, fake, cache, policy


def test_an_llm_seller_looks_up_rival_prices_then_sets_its_price(tmp_path):
    sink, fake, *_ = llm_run(cooperator(), tmp_path)
    assert set(prices(sink, "A").values()) == {JOINT}
    calls = of(sink, "llm_call", agent_id="A")
    assert len(calls) == 12 and {c.purpose for c in calls} == {"act"} and not any(c.cache_hit for c in calls)
    assert [e.tool for e in of(sink, "tool_called", agent_id="A")] == ["get_rival_prices"] * 6
    assert of(sink, "planned", agent_id="A", t=3)[0].plan == "Hold the high price."
    assert of(sink, "planned")[0].tier == "C" and "plan" not in of(sink, "acted", agent_id="A", t=3)[0].model_dump()
    assert calls[0].cost_usd == pytest.approx((100 * 1.0 + 20 * 2.0) / 1e6)


def test_a_rerun_with_the_same_cache_makes_no_request_and_gives_the_same_episode(tmp_path):
    first, _, cache, _ = llm_run(cooperator(), tmp_path)
    second, fake, *_ = llm_run(cooperator(price=5.0), tmp_path, cache=cache, run_id="run_RERUN")
    assert fake.bodies == []
    assert prices(second, "A") == prices(first, "A")
    calls = of(second, "llm_call")
    assert all(c.cache_hit and c.cost_usd == 0 for c in calls)
    assert [c.request_hash for c in calls] == [c.request_hash for c in of(first, "llm_call")]


def test_a_replay_by_the_run_that_paid_counts_its_cost_once(tmp_path):
    first, _, cache, _ = llm_run(cooperator(), tmp_path)
    budget = BudgetGuard()
    replay, fake, *_ = llm_run(cooperator(), tmp_path, cache=cache, budget=budget)
    paid = of(first, "llm_call")
    assert fake.bodies == [] and all(c.cache_hit for c in of(replay, "llm_call"))
    assert [c.cost_usd for c in of(replay, "llm_call")] == [c.cost_usd for c in paid]
    assert budget.calls == len(paid) and budget.spent_usd == pytest.approx(sum(c.cost_usd for c in paid))


def test_an_invalid_reply_is_resent_unchanged_twice_then_the_seller_holds(tmp_path):
    sink, fake, *_ = llm_run(lambda body: call("raise_prices"), tmp_path, rounds=2)
    first_three = [{k: v for k, v in b.items() if k != "seed"} for b in fake.bodies[:3]]
    assert first_three[0] == first_three[1] == first_three[2]
    assert len(fake.bodies) == 6
    calls = of(sink, "llm_call", agent_id="A")
    assert [c.attempt for c in calls] == [1, 2, 3, 1, 2, 3] and {c.error for c in calls} == {"invalid_tool"}
    acted = of(sink, "acted", agent_id="A")
    assert all(a.fallback and a.hold for a in acted)
    assert prices(sink, "A") == {1: NASH, 2: NASH}


def test_provider_errors_are_not_retried_by_the_sdk(tmp_path):
    sink, fake, *_ = llm_run(lambda body: 500, tmp_path, rounds=1)
    assert len(fake.bodies) == 3
    assert {c.error for c in of(sink, "llm_call")} == {"provider_error"}
    assert of(sink, "acted", agent_id="A")[0].fallback


def test_a_tool_that_is_not_granted_is_not_offered(tmp_path):
    seen = []

    def brain(body):
        seen.append(tools_offered(body))
        return call("set_price", price=7.0)

    llm_run(brain, tmp_path, rounds=2, cap=capability(see_rivals=False))
    assert all("get_rival_prices" not in s and "set_price" in s for s in seen)


def test_the_request_holds_only_our_text_and_matches_the_preview(tmp_path):
    sink, fake, _, policy = llm_run(cooperator(), tmp_path, rounds=1)
    body = fake.bodies[0]
    assert body["messages"][0] == {"role": "system", "content": SYSTEM}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    obs = of(sink, "observed", agent_id="A", t=1, purpose="act")[0].observation
    cap = policy.capability
    box = View([], {"A": cap, "B": cap}, False).toolbox("A", 1, RunState(), "act", QueryBudget(3), lambda *a: None)
    assert policy.preview(obs, box, "act") == body


def test_the_key_never_reaches_events_cache_or_bodies(tmp_path):
    sink, fake, cache, _ = llm_run(cooperator(), tmp_path)
    llm_run(lambda body: 500, tmp_path / "err", rounds=1)
    blob = "\n".join(e.model_dump_json() for e in sink.events)
    assert KEY not in blob
    assert all(KEY.encode() not in raw for raw in fake.raw)
    cache.close()
    for f in tmp_path.rglob("*.sqlite*"):
        assert KEY.encode() not in f.read_bytes(), f


def test_the_budget_stops_the_run_before_a_call_it_cannot_pay(tmp_path):
    sink, *_ = llm_run(cooperator(), tmp_path, rounds=10, budget=BudgetGuard(max_calls=5))
    assert len(of(sink, "llm_call")) == 5
    end = of(sink, "run_ended")[0]
    assert end.status == "stopped" and end.end_reason == "budget_exceeded"
    assert of(sink, "budget_warning")[-1].level == "p100"


def test_a_cost_cap_is_never_passed(tmp_path):
    sink, *_ = llm_run(cooperator(), tmp_path, rounds=40, budget=BudgetGuard(max_usd=0.004))
    assert of(sink, "run_ended")[0].end_reason == "budget_exceeded"
    assert sum(c.cost_usd for c in of(sink, "llm_call")) <= 0.004


def test_the_seed_comes_from_run_seller_round_sample_and_attempt(tmp_path):
    _, fake, *_ = llm_run(cooperator(), tmp_path, rounds=1, seed=4)
    assert fake.bodies[0]["seed"] == request_seed(4, "A", 1, 0, 1)
    assert request_seed(4, "A", 1, 0, 1) == int(hashlib.sha256(b"4:A:1:0:1").hexdigest(), 16) % 2**31
    no_seed = PROFILE.model_copy(update={"supports_seed": False})
    _, fake, *_ = llm_run(cooperator(), tmp_path / "b", rounds=1, profile=no_seed)
    assert "seed" not in fake.bodies[0]


def test_one_call_mode_posts_and_prices_in_one_turn(tmp_path):
    def brain(body):
        if "post" in tools_offered(body) and not any(m["role"] == "tool" for m in body["messages"]):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "post", "arguments": json.dumps({"channel_id": "public", "text": "Let us keep 10.5."})},
                    },
                ],
            }
        return call("set_price", price=JOINT)

    sink, *_ = llm_run(brain, tmp_path, rounds=2, cap=capability(channels=["public"]), call_mode="one_call", public=True)
    assert [p.text for p in of(sink, "posted", agent_id="A")] == ["Let us keep 10.5."] * 2
    assert set(prices(sink, "A").values()) == {JOINT}


def test_memory_is_summarised_every_five_rounds_and_shown_later(tmp_path):
    sink, fake, *_ = llm_run(cooperator(), tmp_path, rounds=6)
    (memo,) = of(sink, "memory_summarised", agent_id="A")
    assert memo.t == 5 and memo.covers == (1, 5) and "10.5" in memo.summary
    round6 = [b for b in fake.bodies if "Round 6" in b["messages"][-1].get("content", "") or "Round 6" in json.dumps(b["messages"][1])]
    assert round6 and memo.summary in json.dumps(round6[0]["messages"])


def test_memory_summary_by_llm_records_the_models_note(tmp_path):
    def brain(body):
        if not body.get("tools"):
            return text("I kept 10.5 and so did the rival.")
        return cooperator()(body)

    sink, fake, *_ = llm_run(brain, tmp_path, rounds=5, memory_summary="llm")
    (memo,) = of(sink, "memory_summarised", agent_id="A")
    assert memo.summary == "I kept 10.5 and so did the rival."
    (summary_call,) = of(sink, "llm_call", purpose="summary")
    assert summary_call.t == 5 and not summary_call.error
    assert of(sink, "run_started")[0].header.memory_summary == "llm"


def test_a_failed_llm_summary_falls_back_to_the_code_note(tmp_path):
    def brain(body):
        return 500 if not body.get("tools") else cooperator()(body)

    sink, *_ = llm_run(brain, tmp_path, rounds=5, memory_summary="llm")
    (memo,) = of(sink, "memory_summarised", agent_id="A")
    assert memo.summary.startswith("Rounds 1-5:")


@pytest.mark.parametrize("supports_seed", [True, False])
def test_two_run_seeds_on_one_cache_make_separate_requests(tmp_path, supports_seed):
    profile = PROFILE.model_copy(update={"supports_seed": supports_seed})
    first, fake1, cache, _ = llm_run(cooperator(), tmp_path, rounds=3, profile=profile, seed=1)
    second, fake2, *_ = llm_run(cooperator(), tmp_path, rounds=3, profile=profile, cache=cache, seed=2)
    assert len(fake2.bodies) == len(fake1.bodies) > 0
    assert not any(c.cache_hit for c in of(second, "llm_call"))
    assert not {c.request_hash for c in of(first, "llm_call")} & {c.request_hash for c in of(second, "llm_call")}


def keys(x):
    if isinstance(x, dict):
        return set(x) | {k for v in x.values() for k in keys(v)}
    if isinstance(x, list):
        return {k for v in x for k in keys(v)}
    return set()


def test_tier_a_events_hold_no_model_text_except_posts_and_no_reference_values(tmp_path):
    note = "I kept 10.5 and so did the rival."

    def brain(body):
        if not body.get("tools"):
            return text(note)
        return cooperator()(body)

    sink, *_ = llm_run(brain, tmp_path, rounds=5, memory_summary="llm")
    assert of(sink, "planned") and of(sink, "memory_summarised")
    tier_a = json.dumps([e.model_dump(mode="json") for e in sink.events if e.tier == "A" and e.type != "posted"])
    assert "Hold the high price." not in tier_a and note not in tier_a
    layer_b_c = {
        "reference",
        "best_response",
        "consumer_surplus",
        "nash",
        "joint_optimum",
        "params",
        "costs",
        "plan",
        "summary",
        "memory_summary",
    }
    assert not keys(json.loads(tier_a)) & layer_b_c
    assert {e.tier for e in sink.events if e.type == "reference"} == {"B"} and len(of(sink, "reference")) == len(of(sink, "settled"))


def test_memory_covers_the_configured_window(tmp_path):
    sink, *_ = llm_run(cooperator(), tmp_path, rounds=6, memory_every=3)
    assert [m.covers for m in of(sink, "memory_summarised", agent_id="A")] == [(1, 3), (4, 6)]
    tail = [e.type for e in sink.events if e.t == 3 and e.type in {"settled", "reference", "memory_summarised", "snapshot_saved"}]
    assert tail == ["settled", "reference", "memory_summarised", "snapshot_saved"]


def test_a_profile_never_has_more_calls_in_flight_than_its_max_concurrency():
    import threading
    import time

    from adapters.llm import Request

    lock, live, peak = threading.Lock(), [0], [0]

    def slow(body):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.2)
        with lock:
            live[0] -= 1
        return text("ok")

    profile = PROFILE.model_copy(update={"id": "prof_slots", "max_concurrency": 2})
    fake = FakeLLM(slow)
    req = Request(messages=[{"role": "user", "content": "hi"}], tools=[], allow_text=True)
    threads = [threading.Thread(target=Endpoint(profile, api_key=KEY, transport=fake.transport()).request, args=(req,)) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(fake.bodies) == 6 and peak[0] == 2


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "auth"), (403, "auth"), (404, "not_found"), (410, "not_found"), (422, "provider_error")],
)
def test_a_bad_key_unknown_or_removed_model_or_bad_request_stops_the_run_after_one_call(tmp_path, status, code):
    """DECISIONS-23 entries 1 and 2: 410 Gone (a removed model) is not_found; 401, 403, 404, 410 and 422 are fatal."""
    from adapters.llm import EndpointError

    sent = []
    with pytest.raises(EndpointError) as e:
        llm_run(lambda body: sent.append(body) or status, tmp_path, rounds=3)
    assert (e.value.code, e.value.http_status, e.value.fatal, len(sent)) == (code, status, True, 1)


def test_a_400_refusal_is_resent_then_a_recorded_hold_with_its_status_code_and_redacted_message(tmp_path):
    """DECISIONS-23 entry 2: a 400 keeps the episode (a recorded fallback), with its cause (DECISIONS-24 §3)."""
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX1234"
    body = {"error": {"message": f"Request   refused by policy.\n Echo: {secret}", "code": "content_policy_violation"}}
    sent = []

    def handle(request):
        sent.append(request)
        return httpx2.Response(400, json=body)

    sink, *_ = llm_run(None, tmp_path, rounds=1, transport=httpx2.MockTransport(handle))
    assert len(sent) == 3
    calls = of(sink, "llm_call", agent_id="A")
    assert {(c.error, c.http_status, c.provider_code) for c in calls} == {("provider_error", 400, "content_policy_violation")}
    assert {c.provider_message for c in calls} == {"Request refused by policy. Echo: [redacted]"}
    assert secret not in "".join(e.model_dump_json() for e in sink.events)
    acted = of(sink, "acted", agent_id="A")[0]
    assert (acted.hold, acted.fallback_reason) == (True, "error") and of(sink, "run_ended")[0].status == "completed"


@pytest.mark.parametrize("echo", [KEY[:12] + "*" * 9 + KEY[-4:], KEY[:12] + "…" + KEY[-4:], KEY[8:]])
def test_a_401_that_echoes_the_masked_key_records_no_part_of_it(tmp_path, echo):
    """CR-90 addendum 1: llm_call.provider_message is masked with the endpoint's own key (keys.mask_secrets), so a
    provider's masked echo of it loses its head and its tail (CR-89 addendum 3 finding 1). KEY[8:] has no key shape: only
    the key's own windows mask it."""
    from adapters.llm import EndpointError

    body = {"error": {"message": f"Incorrect API key provided: {echo}. Check the key.", "type": "invalid_request_error"}}
    sink = MemorySink()
    with pytest.raises(EndpointError):
        llm_run(None, tmp_path, rounds=1, sink=sink, transport=httpx2.MockTransport(lambda r: httpx2.Response(401, json=body)))
    (call,) = of(sink, "llm_call")
    assert (call.http_status, call.provider_message) == (401, "Incorrect API key provided: [redacted] Check the key.")
    assert KEY[-4:] not in "".join(e.model_dump_json() for e in sink.events)


@pytest.mark.parametrize("status", [400, 408, 500, 503])  # 429 stops the run (DECISIONS-32)
def test_a_request_timeout_rate_limit_or_server_error_is_resent(tmp_path, status):
    sink, fake, *_ = llm_run(lambda body: status, tmp_path, rounds=1)
    assert len(fake.bodies) == 3 and of(sink, "acted", agent_id="A")[0].fallback_reason == "error"
    assert of(sink, "run_ended")[0].status == "completed"


@pytest.mark.parametrize(
    ("error", "code", "kind"),
    [
        (httpx2.ReadTimeout, "provider_error", "stream_stalled"),  # on a stream, no data for STALL_S (DECISIONS-21 §3)
        (httpx2.ConnectTimeout, "timeout", "APITimeoutError"),
        (httpx2.ConnectError, "provider_error", "APIConnectionError"),
    ],
)
def test_a_network_failure_is_retried_then_the_seller_holds_and_the_run_completes(tmp_path, error, code, kind):
    """A stalled stream is a slow call: one lowered retry, then a hold with reason timeout (DECISIONS-22 entries 5.4 and 7).
    A connect timeout or a connection error is resent unchanged up to 3 attempts."""
    sink, fake, *_ = llm_run(lambda body: error, tmp_path, rounds=2)
    calls = of(sink, "llm_call", agent_id="A")
    slow = kind == "stream_stalled"
    attempts = [1, 2] if slow else [1, 2, 3]
    assert {(c.error, c.provider_code) for c in calls} == {(code, kind)} and [c.attempt for c in calls if c.t == 1] == attempts
    assert all(a.fallback and a.hold and a.fallback_reason == ("timeout" if slow else "error") for a in of(sink, "acted", agent_id="A"))
    assert of(sink, "run_ended")[0].status == "completed"


def test_a_bug_in_the_adapter_is_not_hidden_as_a_provider_error(tmp_path, monkeypatch):
    def broken(*a, **kw):
        raise KeyError("bug")

    monkeypatch.setattr("adapters.llm.endpoint.model_request_stream", broken)
    with pytest.raises(KeyError):
        llm_run(cooperator(), tmp_path, rounds=1)


def test_the_key_is_sent_per_request_and_is_not_held_by_the_client_objects(tmp_path):
    _, fake, *_ = llm_run(cooperator(), tmp_path, rounds=1)
    assert fake.auth == f"Bearer {KEY}"
    endpoint = Endpoint(PROFILE, api_key=KEY, transport=fake.transport())
    client = endpoint._model.client
    shown = [repr(endpoint), repr(vars(endpoint)), str(client.api_key), repr(client._client.auth), repr(vars(client))]
    assert not any(KEY in s for s in shown)


def test_a_text_reply_in_an_act_turn_is_resent_once_with_a_nudge_then_the_seller_holds(tmp_path):
    sink, fake, *_ = llm_run(lambda body: text("I think 9 is good."), tmp_path, rounds=1)
    first, second, third = ({k: v for k, v in b.items() if k != "seed"} for b in fake.bodies)
    assert second["messages"][:-2] == first["messages"] and second["messages"][-1] == {"role": "user", "content": NUDGE}
    assert third == second, "only one nudge"
    assert {c.error for c in of(sink, "llm_call", agent_id="A")} == {"invalid_tool"}
    assert of(sink, "acted", agent_id="A")[0].fallback
