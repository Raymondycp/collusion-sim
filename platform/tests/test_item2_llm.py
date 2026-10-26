"""QUEUE de item 2, adapter level: provider protocols, token kinds, optional prices, max_tokens, the 5-hour window, run limits.

Every endpoint is a fake transport; no paid call.
"""

import json
import threading

import pytest

from adapters.llm import BudgetGuard, CallCache, Endpoint, EndpointError, ProviderProfile, Request
from adapters.llm.budget import WINDOW_S, RateWindow
from fakellm import call, cooperator, tools_offered
from fakewire import WIRE, FakeWire, streamed
from simcore.agents import BudgetExceeded
from simkit import JOINT, capability, of
from simschema.events import RunLimits
from test_llm_policy import KEY, PROFILE, llm_run

BASE = {
    "openai_chat": "http://fake.local/v1",
    "openai_responses": "http://fake.local/v1",
    "anthropic_messages": "http://fake.local/anthropic",
    "google": "http://fake.local/google",
}
HEADER = {
    "openai_chat": "authorization",
    "openai_responses": "authorization",
    "anthropic_messages": "x-api-key",
    "google": "x-goog-api-key",
}
PATH = {
    "openai_chat": "/v1/chat/completions",
    "openai_responses": "/v1/responses",
    "anthropic_messages": "/anthropic/v1/messages",
    "google": "/google/v1beta/models/fake-model:streamGenerateContent",
}
TOOL = {
    "name": "set_price",
    "description": "Set your price.",
    "parameters": {"type": "object", "properties": {"price": {"type": "number"}}},
}


def profile(protocol: str, **kw) -> ProviderProfile:
    return ProviderProfile(id="prof_w", base_url=BASE[protocol], model="fake-model", family="f", protocol=protocol, **kw)


def ask(protocol: str, wire: FakeWire):
    return Endpoint(profile(protocol), KEY, transport=wire.transport()).request(
        Request([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], [TOOL], False)
    )[0]


@pytest.mark.parametrize("protocol", list(WIRE))
def test_each_protocol_sends_its_own_path_and_key_header_and_reads_every_token_kind(protocol):
    wire = FakeWire(protocol)
    reply = ask(protocol, wire)
    assert [(c.name, json.loads(c.arguments)["price"]) for c in reply.tool_calls] == [("set_price", 10.5)]
    assert (reply.input_tokens, reply.cached_input_tokens, reply.output_tokens) == (100, 40, 20)
    # Anthropic's API does not report thinking tokens apart from output, so 0 there.
    assert reply.reasoning_tokens == (0 if protocol == "anthropic_messages" else 5)
    req = wire.requests[0]
    assert req.url.path == PATH[protocol]
    assert [k for k, v in req.headers.items() if KEY in v] == [HEADER[protocol]]


@pytest.mark.parametrize("protocol", list(WIRE))
def test_each_protocol_maps_a_429_to_a_code_with_one_request_only(protocol):
    wire = FakeWire(protocol, status=429)
    with pytest.raises(EndpointError) as e:
        ask(protocol, wire)
    assert e.value.code == "rate_limit" and len(wire.requests) == 1 and KEY not in str(e.value)


def test_a_registered_protocol_with_no_client_refuses_to_build():
    with pytest.raises(EndpointError) as e:
        Endpoint(profile("openai_chat").model_copy(update={"protocol": "typesafe_systemone"}), KEY)
    assert e.value.code == "protocol_not_built"


def test_a_google_rerun_with_the_same_cache_sends_nothing(tmp_path):
    """Google sends no tool-call id and Pydantic AI makes a random one. The cached reply keeps that id, so a rerun matches."""

    class TwoStep(FakeWire):
        def _handle(self, request):
            body = json.loads(request.content)
            if not any("functionResponse" in p for c in body["contents"] for p in c.get("parts", [])):
                self.requests.append(request)
                return streamed(
                    "google",
                    {
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"functionCall": {"name": "get_rival_prices", "args": {}}}]},
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
                    },
                )
            return super()._handle(request)

    g = profile("google", input_usd_per_mtok=1.0, output_usd_per_mtok=2.0)
    cache = CallCache(tmp_path / "calls.sqlite")
    first, second = TwoStep("google"), TwoStep("google")
    llm_run(None, tmp_path, rounds=3, profile=g, cache=cache, transport=first.transport())
    sink, *_ = llm_run(None, tmp_path, rounds=3, profile=g, cache=cache, transport=second.transport())
    assert len(first.requests) == 6 and second.requests == []
    assert all(c.cache_hit for c in of(sink, "llm_call"))


def test_a_call_records_cached_and_reasoning_tokens_and_prices_cached_input_at_its_own_price(tmp_path):
    p = profile("openai_chat", input_usd_per_mtok=1.0, output_usd_per_mtok=2.0, cached_input_usd_per_mtok=0.1)
    sink, *_ = llm_run(None, tmp_path, rounds=1, profile=p, transport=FakeWire("openai_chat").transport())
    c = of(sink, "llm_call")[0]
    assert (c.input_tokens, c.cached_input_tokens, c.output_tokens, c.reasoning_tokens) == (100, 40, 20, 5)
    assert c.cost_usd == pytest.approx((60 * 1.0 + 40 * 0.1 + 20 * 2.0) / 1e6)


def test_without_a_price_calls_cost_null_and_only_the_call_and_token_caps_act(tmp_path):
    unpriced = PROFILE.model_copy(update={"input_usd_per_mtok": None, "output_usd_per_mtok": None})
    guard = BudgetGuard(max_usd=0.000001, max_calls=4)
    sink, *_ = llm_run(cooperator(), tmp_path, rounds=10, profile=unpriced, budget=guard)
    calls = of(sink, "llm_call")
    assert len(calls) == 4 and all(c.cost_usd is None for c in calls)
    assert guard.unpriced == 4 and guard.spent_usd == 0
    assert of(sink, "run_ended")[0].end_reason == "budget_exceeded"


def test_max_tokens_stops_the_run_before_a_call_that_could_pass_it(tmp_path):
    guard = BudgetGuard(max_tokens=5000)
    sink, *_ = llm_run(cooperator(), tmp_path, rounds=40, budget=guard)
    calls = of(sink, "llm_call")
    assert calls and guard.tokens == sum(c.input_tokens + c.output_tokens for c in calls) <= 5000
    assert of(sink, "run_ended")[0].end_reason == "budget_exceeded"


def test_a_full_5_hour_window_ends_the_run_with_its_own_reason_and_old_calls_leave_it(tmp_path):
    now = [10 * WINDOW_S]
    window = RateWindow(5, [now[0] - WINDOW_S - 1, now[0] - 10, now[0] - 5], clock=lambda: now[0])
    assert window.room() == 3
    batch = BudgetGuard(max_calls=100)
    guard = BudgetGuard(max_calls=100, batch=batch)
    sink, *_ = llm_run(cooperator(), tmp_path, rounds=10, budget=guard, policy_kw={"window": window})
    assert len(of(sink, "llm_call")) == 3
    end = of(sink, "run_ended")[0]
    assert end.status == "stopped" and end.end_reason == "rate_window"
    assert of(sink, "budget_warning") == []  # CR-19 m1: a window stop is not a budget stop
    assert guard.calls == batch.calls == 3 and guard.held_tokens == batch.held_tokens == 0  # m2: the refused call counts nowhere
    now[0] += WINDOW_S
    assert window.room() == 5


@pytest.mark.parametrize("cap", ["max_tokens", "max_usd"])
@pytest.mark.parametrize("shared", [False, True])
def test_calls_in_flight_at_the_same_time_cannot_pass_a_cap_together(cap, shared):
    """CR-19 M1: each call holds its estimate from reserve to spend. Room for one call: two at once, one passes."""
    limit = {"max_tokens": 1000, "max_usd": 1.0}[cap]
    batch = BudgetGuard(**{cap: limit}) if shared else None
    guards = [BudgetGuard(batch=batch) for _ in range(2)] if shared else [BudgetGuard(**{cap: limit})] * 2
    reserved, gate = [], threading.Barrier(2)

    def call(g):
        try:
            reserved.append((g, g.reserve(0.6, 600)))
        except BudgetExceeded:
            pass
        gate.wait()  # both have tried before either reply comes back

    threads = [threading.Thread(target=call, args=(g,)) for g in guards]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(reserved) == 1
    capped = batch or guards[0]
    assert (capped.held_tokens, capped.held_usd) == (600, 0.6)
    g, hold = reserved[0]
    g.spend(0.5, 500, hold)
    assert (capped.held_tokens, capped.held_usd, capped.tokens, capped.spent_usd) == (0, 0.0, 500, 0.5)


def test_a_cancelled_hold_gives_back_the_call_and_its_room():
    batch = BudgetGuard(max_calls=1, max_tokens=1000)
    g = BudgetGuard(batch=batch)
    hold = g.reserve(None, 900)
    with pytest.raises(BudgetExceeded):
        BudgetGuard(batch=batch).reserve(None, 900)
    g.cancel(hold)
    assert (g.calls, batch.calls, batch.held_tokens) == (0, 0, 0)
    g.reserve(None, 900)


def test_a_failed_cache_write_still_counts_the_paid_call_and_frees_its_hold(tmp_path):
    class FullDisk(CallCache):
        def put(self, *a, **kw):
            raise OSError("disk full")

    guard = BudgetGuard(max_tokens=100_000)
    try:
        llm_run(cooperator(), tmp_path, cache=FullDisk(tmp_path / "calls.sqlite"), budget=guard)
    except OSError:
        pass
    assert guard.calls == 1 and guard.tokens > 0 and (guard.held_tokens, guard.held_usd) == (0, 0.0)


def test_a_turn_that_uses_every_step_is_flagged_on_acted(tmp_path):
    def looker(body):
        return call("get_news")

    sink, *_ = llm_run(looker, tmp_path, rounds=1, limits=RunLimits(max_steps_per_turn=2), policy_kw={"max_steps": 2})
    acted = of(sink, "acted", agent_id="A")[0]
    assert (acted.steps, acted.tool_calls, acted.fallback) == (2, 2, True)
    assert acted.limits_hit == ["max_steps_per_turn"]
    assert of(sink, "acted", agent_id="B")[0].steps is None


def two_posts(body):
    if "post" in tools_offered(body) and not any(m["role"] == "tool" for m in body["messages"]):
        posts = [{"channel_id": "public", "text": t} for t in ("One.", "Two.")]
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "post", "arguments": json.dumps(p)}} for i, p in enumerate(posts)
            ],
        }
    return call("set_price", price=JOINT)


@pytest.mark.parametrize(
    "run_limit, grant_limit, sent, name",
    [(1, 2, 1, "max_messages_per_round"), (2, 1, 1, "post.max_per_round"), (2, 2, 2, None)],
)
def test_the_smaller_message_limit_applies_and_acted_names_it(tmp_path, run_limit, grant_limit, sent, name):
    limits = RunLimits(max_messages_per_round=run_limit)
    cap = capability(channels=["public"])
    cap = cap.model_copy(
        update={
            "grants": [
                g.model_copy(update={"params": {**g.params, "max_per_round": grant_limit}}) if g.tool_id == "post" else g
                for g in cap.grants
            ]
        }
    )
    sink, *_ = llm_run(
        two_posts,
        tmp_path,
        rounds=1,
        cap=cap,
        call_mode="one_call",
        public=True,
        limits=limits,
        policy_kw={"max_messages": limits.messages(grant_limit)},
    )
    assert [p.text for p in of(sink, "posted", agent_id="A")] == ["One.", "Two."][:sent]
    acted = of(sink, "acted", agent_id="A")[0]
    assert acted.messages == sent and acted.limits_hit == ([name] if name else [])


def test_the_run_limit_lowers_the_query_budget_and_the_read_tools_are_no_longer_offered(tmp_path):
    """PILOT-1c fix (1): a used budget removes the read tools from the offer, so no read is refused."""
    offers = []

    def reads_when_offered(body):
        offers.append(tools_offered(body))
        return call("get_rival_prices", rounds=1) if "get_rival_prices" in offers[-1] else call("set_price", price=JOINT)

    sink, *_ = llm_run(reads_when_offered, tmp_path, rounds=1, limits=RunLimits(max_queries_per_round=1))
    assert [e.allowed for e in of(sink, "tool_called", agent_id="A")] == [True]
    assert offers[1] == ["set_price", "hold"]
    assert not of(sink, "acted", agent_id="A")[0].fallback


@pytest.mark.parametrize("status, retried", [(429, False), (500, True)])
def test_a_provider_429_stops_the_run_at_once_while_other_errors_are_retried(tmp_path, status, retried):
    """24 Oct test run: a quota 429 was retried and the sellers held for 45 minutes. A 429 now ends the run (rate_window)."""
    sink, *_ = llm_run(lambda body: status, tmp_path, rounds=3)
    calls = of(sink, "llm_call")
    end = of(sink, "run_ended")[0]
    if retried:
        assert end.end_reason == "horizon" and any(c.attempt == 3 for c in calls)
    else:
        assert end.status == "stopped" and end.end_reason == "rate_window"
        assert [c.http_status for c in calls] == [429] and of(sink, "acted") == []
