# ruff: noqa: F401, F811 (the fixtures root and fake_client come from test_item2_api and test_d13 through test_budget)
"""DECISIONS-22 (entries 5 and 6) and the tests of CR-88 §4 A-E and G: the timeout fallback, the replayed timeout, the
parallel seller calls of a phase, the threads, the talk_thinking cut and the market facts. Fake endpoints only."""

import gc
import json
import random
import re
import threading
import time
import warnings

import httpx2
import pytest

from adapters.llm import BudgetGuard, CallCache, Endpoint, LLMPolicy
from adapters.llm.endpoint import ProviderProfile
from adapters.llm.policy import NUDGE, market_facts
from fakellm import FakeLLM, call, cooperator, text, tools_offered
from features import REGISTRY
from features.table import row_values
from judge.views import HEAD, TAIL, NotJudged, cut, render
from services import models_file
from simcore.market import PriceGrid
from simcore.recorder import MemorySink, Recorder
from simcore.recorder import _hash_line as recorder_hash_line
from simcore.run import Run
from simkit import JOINT, capability, frozen_scenario, of, prices, spec
from simschema.events import AgentHeader, BranchInfo, ProbeIntervention
from test_budget import env_run
from test_item2_api import root
from test_llm_policy import KEY, PROFILE, SYSTEM, llm_run
from test_trail_access import episode, trail_line

TIMING = ("wall_time", "latency_ms", "slot_wait_ms", "first_byte_ms", "episode_hash")
"""Measured times differ between two live runs of the same episode, so the determinism checks leave them out, and the
episode hash with them: it covers latency_ms (as before D22). A replay from the cache gives the same hash (test c1)."""


STALL = httpx2.ReadTimeout
"""On a stream, a read timeout is stream_stalled: a slow call, like the deadline (policy.SLOW)."""


def is_talk(body: dict) -> bool:
    return "set_price" not in tools_offered(body)


def round_of(body: dict) -> int:
    user = next(m["content"] for m in body["messages"] if m["role"] == "user")
    return int(re.search(r"Round (\d+)\.", user).group(1))


def effort(fake: FakeLLM) -> list:
    return [b.get("reasoning_effort") for b in fake.bodies]


def plain(body: dict) -> dict:
    """Talk: PASS. Act: set the joint price, with no look-up."""
    return text("PASS") if is_talk(body) else call("set_price", price=JOINT)


def talker(body: dict) -> dict:
    return text("I hold 10.5.") if is_talk(body) else call("set_price", price=JOINT)


def script(*first, then=plain):
    """The replies of the first requests, in order, then `then`."""
    queue = list(first)

    def brain(body):
        return queue.pop(0) if queue else then(body)

    return brain


def episode_lines(sink, drop=TIMING) -> list[str]:
    return [e.model_dump_json(exclude=set(drop)) for e in sink.events]


# Two LLM sellers (CR-88 §4 C and D)


def header(sid: str, cap, profile=PROFILE) -> AgentHeader:
    return AgentHeader(
        id=sid,
        role="seller",
        policy_kind="llm",
        capability=cap,
        provider_profile_id=profile.id,
        base_url_host="fake.local",
        model=profile.model,
        temperature=0.7,
    )


class Duo:
    def __init__(self, sink, fakes, cache, policies, error):
        self.sink, self.fakes, self.cache, self.policies, self.error = sink, fakes, cache, policies, error


def duo(
    brains,
    tmp_path,
    rounds=3,
    cache=None,
    budget=None,
    delay=None,
    parallel=True,
    branch=None,
    profile=PROFILE,
    policy_kw=None,
    call_mode="talk_and_act",
    timeout_s=300,
    queries=3,
    **kw,
) -> Duo:
    """A and B both LLM sellers on their own fake endpoint, sharing the cache and the budget. `delay(seller, body)`: the
    seconds the fake waits before it answers. `parallel=False` runs the phases as before D22 (the reference order). A
    raised error is kept in Duo.error with the events recorded up to it."""
    cap = capability(channels=("private_A_B",), see_rivals=False, queries=queries)
    sp = spec({"A": cap, "B": cap}, {"A": "llm", "B": "llm"}, call_mode=call_mode, private_pairs=True, **kw)
    sp = sp.model_copy(update={"agents": [header(a.id, a.capability, profile) for a in sp.agents]})
    sink = MemorySink()
    rec = Recorder(sp.run_id, [sink], now=lambda: "2026-10-22T00:00:00.000Z")
    cache = cache or CallCache(tmp_path / "calls.sqlite")
    budget = budget or BudgetGuard()

    def wired(sid, brain):
        def answer(body):
            if delay:
                time.sleep(delay(sid, body))
            return brain(body)

        return FakeLLM(answer)

    fakes = {s: wired(s, b) for s, b in brains.items()}
    policies = {
        s: LLMPolicy(
            seller_id=s,
            endpoint=Endpoint(profile, api_key=KEY, transport=fakes[s].transport(), timeout_s=timeout_s),
            system_prompt=SYSTEM,
            capability=cap,
            recorder=rec,
            cache=cache,
            budget=budget,
            run_seed=sp.seed,
            memory_summary=sp.memory_summary,
            **(policy_kw or {}),
        )
        for s in brains
    }
    scenario = frozen_scenario(rounds=rounds)
    start = branch.fork_t if branch else 1
    run = Run(sp, scenario, policies, rec, branch=branch, rounds=range(start, start + rounds))
    run._parallel = parallel
    error = None
    try:
        run.execute()
    except Exception as e:  # noqa: BLE001 (the test reads it)
        error = e
    return Duo(sink, fakes, cache, policies, error)


def jitter(seed: int, top: float = 0.004):
    """Seeded random waits per seller: which call finishes first changes from call to call and between repetitions."""
    rngs = {s: random.Random(f"{seed}:{s}") for s in "AB"}
    lock = threading.Lock()

    def delay(sid, body):
        with lock:
            return rngs[sid].uniform(0, top)

    return delay


# A. Retry and fallback (rows 3 to 7)


def test_a1_a_timeout_then_a_reply_is_one_degraded_call_at_the_lower_thinking(tmp_path):
    brain = script(STALL, then=cooperator())
    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, policy_kw={"thinking": "high", "thinking_on_timeout": "low"})
    calls = of(sink, "llm_call", agent_id="A")
    assert [(c.attempt, c.error, c.degraded) for c in calls] == [(1, "provider_error", None), (2, None, True), (1, None, None)]
    assert effort(fake) == ["high", "low", "high"], "only the attempt after the timeout is lowered; the next step is not"
    acted = of(sink, "acted", agent_id="A")[0]
    assert (acted.price, acted.fallback, acted.fallback_reason) == (JOINT, False, None)


def test_a1_the_same_level_on_timeout_is_not_degraded(tmp_path):
    brain = script(STALL, then=cooperator())
    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, policy_kw={"thinking": "low", "thinking_on_timeout": None})
    assert effort(fake)[:2] == ["low", "low"], "null thinking_on_timeout keeps the thinking"
    assert [c.degraded for c in of(sink, "llm_call", agent_id="A")] == [None, None, None]


def test_a2_two_timeouts_hold_the_act_call_with_reason_timeout(tmp_path):
    sink, fake, *_ = llm_run(lambda body: STALL, tmp_path, rounds=2, policy_kw={"thinking_on_timeout": "minimal"})
    assert len(fake.bodies) == 4, "two requests per round, no third"
    assert effort(fake) == [None, "minimal"] * 2
    for a in of(sink, "acted", agent_id="A"):
        assert (a.hold, a.fallback, a.fallback_reason) == (True, True, "timeout")


def test_a2_two_timeouts_of_a_talk_call_record_a_pass_with_reason_timeout(tmp_path):
    caps = capability(channels=("private_A_B",))
    other = ("scripted_talker", {"price": JOINT, "offer_text": "B says hold 10.5", "channel_id": "private_A_B"}, caps)
    brain = lambda body: STALL if is_talk(body) else call("set_price", price=JOINT)  # noqa: E731
    sink, fake, *_ = llm_run(brain, tmp_path, rounds=2, cap=caps, b=other, call_mode="talk_and_act", private_pairs=True)
    assert sum(is_talk(b) for b in fake.bodies) == 4
    replied = of(sink, "talk_replied", agent_id="A")
    assert [(r.outcome, r.reason, r.text) for r in replied] == [("pass", "timeout", None)] * 2
    assert not of(sink, "posted", agent_id="A")
    assert [a.fallback for a in of(sink, "acted", agent_id="A")] == [False, False], "the act call is not affected"


def test_a3_a_text_reply_then_a_timeout_gets_the_nudge_then_the_lower_level_in_3_attempts(tmp_path):
    brain = script(text("I think 10."), STALL, then=cooperator())
    sink, fake, *_ = llm_run(
        brain, tmp_path, rounds=1, cap=capability(see_rivals=False), policy_kw={"thinking": "high", "thinking_on_timeout": "low"}
    )
    calls = of(sink, "llm_call", agent_id="A")
    assert [(c.attempt, c.error, c.degraded) for c in calls] == [(1, "invalid_tool", None), (2, "provider_error", None), (3, None, True)]
    assert effort(fake) == ["high", "high", "low"]
    nudged = [b["messages"][-1]["content"] for b in fake.bodies[1:]]
    assert nudged == [NUDGE, NUDGE], "the lowered attempt resends the nudge request"
    assert of(sink, "acted", agent_id="A")[0].price == JOINT


def test_a3_a_text_reply_then_two_timeouts_end_after_3_attempts_with_reason_timeout(tmp_path):
    brain = script(text("I think 10."), STALL, STALL, then=cooperator())
    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, policy_kw={"thinking_on_timeout": "low"})
    assert len(fake.bodies) == 3
    assert of(sink, "acted", agent_id="A")[0].fallback_reason == "timeout"


def test_a3_a_timeout_then_a_text_reply_sends_no_nudge_without_an_attempt_left(tmp_path):
    """Attempt 2 (lowered) replies with text; attempt 3 gets the nudge at the lower level and still replies text."""
    brain = script(STALL, text("no"), text("no"), then=cooperator())
    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, policy_kw={"thinking": "high", "thinking_on_timeout": "low"})
    assert len(fake.bodies) == 3 and effort(fake) == ["high", "low", "low"]
    assert fake.bodies[2]["messages"][-1]["content"] == NUDGE
    assert of(sink, "acted", agent_id="A")[0].fallback_reason == "invalid"


@pytest.mark.parametrize(("reply", "reason"), [(text("no tool"), "invalid"), (500, "error")])
def test_a4_an_invalid_reply_or_a_provider_error_never_lowers_the_thinking(tmp_path, reply, reason):
    sink, fake, *_ = llm_run(lambda body: reply, tmp_path, rounds=1, policy_kw={"thinking": "high", "thinking_on_timeout": "low"})
    assert effort(fake) == ["high"] * 3
    assert not any(c.degraded for c in of(sink, "llm_call"))
    assert of(sink, "acted", agent_id="A")[0].fallback_reason == reason


def test_a5_thinking_on_timeout_is_a_frozen_sampling_field_of_models_yaml():
    assert "thinking_on_timeout" in models_file.SAMPLING
    assert "thinking_on_timeout" in models_file.SellerEntry.model_fields
    assert models_file.SellerEntry.model_fields["thinking_on_timeout"].default is None, "null keeps thinking (CR-91 minor 1)"


# B. Replay


def test_b_a_rerun_from_the_cache_replays_the_timeout_hold_with_no_request(tmp_path):
    first, _, cache, _ = llm_run(lambda body: STALL, tmp_path, rounds=2, policy_kw={"thinking_on_timeout": "low"})
    second, fake, *_ = llm_run(cooperator(), tmp_path, rounds=2, cache=cache, policy_kw={"thinking_on_timeout": "low"})
    assert fake.bodies == [], "the provider now answers, but a recorded timeout is an outcome"
    keep = TIMING + ("cache_hit", "episode_hash")  # a live call and its replay differ in cache_hit, so in the hash
    assert episode_lines(second, keep) == episode_lines(first, keep)
    assert all(c.cache_hit and c.provider_code == "stream_stalled" for c in of(second, "llm_call"))
    with cache._lock, cache._conn:
        cache._conn.execute("DELETE FROM timeouts")
    third, fake, *_ = llm_run(cooperator(), tmp_path, rounds=2, cache=cache, policy_kw={"thinking_on_timeout": "low"})
    assert fake.bodies and set(prices(third, "A").values()) == {JOINT}, "without the timeout record the request is sent"


def test_b_another_sample_index_tries_a_recorded_timeout_cell_again(tmp_path):
    """CR-88 addendum 3 point 7: the way to a fresh try of a cell with a recorded slow call."""
    _, _, cache, _ = llm_run(lambda body: STALL, tmp_path, rounds=1)
    again, fake, *_ = llm_run(cooperator(), tmp_path, rounds=1, cache=cache)
    assert fake.bodies == [] and of(again, "acted", agent_id="A")[0].fallback_reason == "timeout"
    fresh, fake, *_ = llm_run(cooperator(), tmp_path, rounds=1, cache=cache, policy_kw={"sample_index": 1})
    assert fake.bodies and of(fresh, "acted", agent_id="A")[0].price == JOINT


def test_a_connect_timeout_is_resent_unchanged_and_never_recorded(tmp_path):
    """CR-88 addendum 3 point 2: only a slow call (deadline, stall) takes the lowered retry and the record."""
    brain = script(httpx2.ConnectTimeout, then=cooperator())
    sink, fake, cache, _ = llm_run(brain, tmp_path, rounds=1, policy_kw={"thinking": "high", "thinking_on_timeout": "low"})
    assert effort(fake)[:2] == ["high", "high"]
    calls = of(sink, "llm_call", agent_id="A")
    assert [(c.error, c.provider_code, c.degraded) for c in calls[:2]] == [("timeout", "APITimeoutError", None), (None, None, None)]
    with cache._lock:
        assert cache._conn.execute("SELECT COUNT(*) FROM timeouts").fetchone() == (0,)
    sink, fake, *_ = llm_run(lambda body: httpx2.ConnectTimeout, tmp_path / "all", rounds=1, policy_kw={"thinking_on_timeout": "low"})
    assert len(fake.bodies) == 3 and of(sink, "acted", agent_id="A")[0].fallback_reason == "error"


def test_a_call_past_the_deadline_takes_the_timeout_path(tmp_path):
    """The deadline itself (call_timeout): a fake that answers after the client timeout."""
    slow = {"n": 0}

    def late(sid, body):
        if sid == "A" and not is_talk(body):
            slow["n"] += 1
            return 1.0 if slow["n"] == 1 else 0
        return 0

    d = duo({"A": plain, "B": plain}, tmp_path, rounds=1, delay=late, timeout_s=0.3, policy_kw={"thinking_on_timeout": "low"})
    assert d.error is None
    calls = [c for c in of(d.sink, "llm_call", agent_id="A") if c.purpose == "act"]
    assert [(c.error, c.provider_code, c.degraded) for c in calls] == [("timeout", "call_timeout", None), (None, None, True)]
    with d.cache._lock:
        assert d.cache._conn.execute("SELECT provider_code FROM timeouts").fetchall() == [("call_timeout",)]


# C. Parallel determinism


def test_c1_c3_twenty_repetitions_with_random_waits_and_timeouts_give_the_same_episode(tmp_path):
    """30 rounds; A's act call times out once in round 4 (degraded retry) and twice in round 9 (hold). The same episode
    every time, and the same as the order of a sequential run."""
    reference = None
    for rep in range(21):
        seen: dict = {}

        def brain_a(body, seen=seen):
            t = round_of(body)
            if not is_talk(body):
                n = seen[t] = seen.get(t, 0) + 1
                if (t == 4 and n == 1) or (t == 9 and n <= 2):
                    return STALL
            return talker(body)

        d = duo(
            {"A": brain_a, "B": talker},
            tmp_path / str(rep),
            rounds=30,
            delay=jitter(rep) if rep else None,
            parallel=rep > 0,
            policy_kw={"thinking_on_timeout": "low"},
        )
        assert d.error is None
        lines = episode_lines(d.sink, TIMING + ("parallel",))  # rep 0 records no path
        if reference is None:
            reference = lines  # rep 0: the sequential order of the calls (before D22), no waits
            assert [a.fallback_reason for a in of(d.sink, "acted", agent_id="A", t=9)] == ["timeout"]
            assert [c.degraded for c in of(d.sink, "llm_call", agent_id="A", t=4) if c.purpose == "act"] == [None, True]
        assert lines == reference, f"repetition {rep}"
    hashes = set()
    for rep in range(3):
        d = duo({"A": talker, "B": talker}, tmp_path / "1", rounds=30, cache=None if rep == 0 else d.cache, delay=jitter(100 + rep))
        if rep:
            assert not any(f.bodies for f in d.fakes.values())
            hashes.add(of(d.sink, "run_ended")[0].episode_hash)
    assert len(hashes) == 1, "a replay from the cache gives the same episode hash whichever call finishes first"


EXPECTED_ROUND = [
    ("observed", "A"),
    ("llm_call", "A"),
    ("talk_replied", "A"),
    ("posted", "A"),
    ("observed", "B"),
    ("llm_call", "B"),
    ("talk_replied", "B"),
    ("posted", "B"),
    ("observed", "A"),
    ("llm_call", "A"),
    ("acted", "A"),
    ("observed", "B"),
    ("llm_call", "B"),
    ("acted", "B"),
]
"""Round 2 of a talk_and_act run, sellers A and B, as entry 5.6 states it: each phase in seller order, each seller's
call events before its result."""


def test_c2_the_event_order_of_a_round_is_the_table_whichever_seller_finishes_first(tmp_path):
    for name, delay in (("a_slow", lambda s, b: 0.05 if s == "A" else 0), ("b_slow", lambda s, b: 0.05 if s == "B" else 0)):
        d = duo({"A": talker, "B": talker}, tmp_path / name, rounds=2, delay=delay)
        got = [(e.type, getattr(e, "agent_id", None)) for e in d.sink.events if e.t == 2]
        assert [x for x in got if x[1] in ("A", "B")] == EXPECTED_ROUND, name


@pytest.mark.parametrize("slow", ["A", "B"])
def test_c4_a_fatal_error_gives_the_same_events_whichever_call_finishes_first(tmp_path, slow):
    def a_brain(body):
        return 401 if round_of(body) == 2 and not is_talk(body) else plain(body)

    runs = []
    for i, delay in enumerate((lambda s, b: 0.05 if s == slow else 0, lambda s, b: 0)):
        runs.append(duo({"A": a_brain, "B": plain}, tmp_path / f"{slow}{i}", rounds=3, delay=delay))
    assert all(d.error is not None for d in runs)
    assert episode_lines(runs[0].sink) == episode_lines(runs[1].sink)
    assert not of(runs[0].sink, "acted", t=2), "no price of the failed phase is set"


def test_c5_a_budget_limit_stops_the_same_seller_every_time(tmp_path):
    """max_calls 7: the parallel check finds no room, so the phase runs one seller after the other; the run stops at
    the same call as the sequential run."""
    lines = []
    for rep in range(6):
        d = duo(
            {"A": plain, "B": plain},
            tmp_path / str(rep),
            rounds=4,
            budget=BudgetGuard(max_calls=7),
            delay=jitter(rep, 0.01) if rep else None,
            parallel=rep > 0,
        )
        assert of(d.sink, "run_ended")[0].end_reason == "budget_exceeded"
        lines.append(episode_lines(d.sink, TIMING + ("parallel",)))
    assert all(x == lines[0] for x in lines)
    calls = [(c.t, c.agent_id, c.purpose) for c in of(d.sink, "llm_call")]
    assert calls[-1] == (2, "A", "act") and len(calls) == 7, "the queries give 5 steps: no room, one seller after the other"
    assert {c.parallel for c in of(d.sink, "llm_call")} == {False}


def test_c5_with_no_queries_a_phase_needs_3_requests_a_seller_so_a_12_call_budget_runs_parallel(tmp_path):
    """CR-88 addendum 3 point 3: one step per call with no query budget (the research conditions), so the model check's
    12 calls run 4 phases in parallel; the 5th has no room for 6 and runs one seller after the other. Each llm_call
    records the path."""
    d = duo({"A": plain, "B": plain}, tmp_path, rounds=4, budget=BudgetGuard(max_calls=12), queries=0)
    path = [(c.t, c.purpose, c.agent_id, c.parallel) for c in of(d.sink, "llm_call")]
    assert path[:8] == [(t, p, s, True) for t in (1, 2) for p in ("talk", "act") for s in "AB"]
    assert path[8:] == [(3, "talk", "A", False), (3, "talk", "B", False), (3, "act", "A", False), (3, "act", "B", False)]
    assert of(d.sink, "run_ended")[0].end_reason == "budget_exceeded"


def test_c5_on_the_one_after_the_other_path_a_failure_stops_the_phase_before_the_next_seller_calls(tmp_path):
    """max_calls 20 has no room for two worst cases (5 steps x 3 attempts each), so A calls first; its fatal 401 in the
    round 2 act phase ends the run before B sends its act request."""

    def a_brain(body):
        return 401 if round_of(body) == 2 and not is_talk(body) else plain(body)

    d = duo({"A": a_brain, "B": plain}, tmp_path, rounds=3, budget=BudgetGuard(max_calls=20))
    assert d.error is not None and {c.parallel for c in of(d.sink, "llm_call")} == {False}
    assert [(round_of(b), is_talk(b)) for b in d.fakes["B"].bodies] == [(1, True), (1, False), (2, True)], "no round 2 act of B"


def test_c6_budget_warnings_are_recorded_after_the_phase_in_the_same_order(tmp_path):
    """The p80 warning is crossed inside a parallel phase and recorded after the phase's last event, with what the run
    spent at the phase end. The same episode with any waits."""
    priced = PROFILE.model_copy(update={"id": "prof_out", "input_usd_per_mtok": 0.0, "output_usd_per_mtok": 1000.0})
    lines = []
    for rep in range(3):
        budget = BudgetGuard(max_usd=2.0)  # 0.02 a call and a hold of the same: p80 after 80 calls, room for 20 more
        kw = dict(rounds=22, budget=budget, queries=0, profile=priced, policy_kw={"max_output_tokens": 20}, delay=jitter(rep, 0.004))
        d = duo({"A": plain, "B": plain}, tmp_path / str(rep), **kw)
        lines.append(episode_lines(d.sink))
        warned = of(d.sink, "budget_warning")
        assert [w.level for w in warned] == ["p80"]
        p80 = warned[0]
        before = [e for e in d.sink.events[: p80.seq] if e.type == "llm_call"][-1]
        assert before.parallel is True, "the p80 phase ran in parallel"
        assert d.sink.events[p80.seq - 1].type in ("talk_replied", "talk_text", "posted", "acted"), "a warning closes a phase"
        assert d.sink.events[p80.seq + 1].type in ("observed", "settled")
        assert p80.spent_usd == pytest.approx(sum(c.cost_usd for c in of(d.sink, "llm_call") if c.seq < p80.seq))
    assert all(x == lines[0] for x in lines)


def test_c7_a_probe_round_makes_one_act_call_not_two(tmp_path):
    probe = BranchInfo(parent_run_id="run_PARENT", fork_t=1, intervention=ProbeIntervention(target="A"))
    d = duo({"A": plain, "B": plain}, tmp_path, rounds=1, branch=probe)
    assert d.error is None
    acts = [c.agent_id for c in of(d.sink, "llm_call") if c.purpose == "act"]
    assert acts == ["B"]


def test_c8_a_sellers_request_does_not_depend_on_whether_the_other_call_has_finished(tmp_path):
    bodies = {}
    for name, delay in (("a_slow", lambda s, b: 0.03 if s == "A" else 0), ("a_fast", lambda s, b: 0 if s == "A" else 0.03)):
        d = duo({"A": talker, "B": talker}, tmp_path / name, rounds=3, delay=delay)
        bodies[name] = [json.dumps(b, sort_keys=True) for b in d.fakes["B"].bodies]
    assert bodies["a_slow"] == bodies["a_fast"]


# D. Threads


def test_d_each_seller_keeps_one_thread_for_30_rounds_with_no_loop_error(tmp_path):
    threads = {"A": set(), "B": set()}

    def watch(sid, body):
        threads[sid].add(threading.current_thread().name)
        return 0

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        d = duo({"A": talker, "B": talker}, tmp_path, rounds=30, delay=watch)
        gc.collect()
    assert d.error is None
    assert len(threads["A"]) == 1 and len(threads["B"]) == 1 and threads["A"] != threads["B"]
    assert not [w for w in caught if "loop" in str(w.message).lower() or "never awaited" in str(w.message)]
    alive = [t.name for t in threading.enumerate() if t.name.startswith("seller-")]
    assert not alive, "the run shuts its seller threads"


def test_d_one_concurrency_slot_makes_the_sellers_wait_for_each_other(tmp_path):
    """One request per seller per round (one_call). Two slots: a round takes one wait; one slot: two waits."""
    wait = 0.4
    took = {}
    for slots in (2, 1):
        profile = PROFILE.model_copy(update={"id": f"prof_slots{slots}", "max_concurrency": slots})
        start = time.perf_counter()
        d = duo({"A": plain, "B": plain}, tmp_path / str(slots), rounds=2, profile=profile, delay=lambda s, b: wait, call_mode="one_call")
        took[slots] = time.perf_counter() - start
        assert d.error is None and len(of(d.sink, "llm_call")) == 4
        waits = [c.slot_wait_ms for c in of(d.sink, "llm_call")]
        assert all(w is not None for w in waits)
        if slots == 1:
            assert max(waits) >= wait * 1000 * 0.8, "the second seller waited for the slot"
    assert took[1] >= 4 * wait and took[1] - took[2] >= 2 * wait * 0.6, took


# E. Judge and blinding


@pytest.mark.parametrize("n", [HEAD + TAIL - 1, HEAD + TAIL, HEAD + TAIL + 1, 94_000])
def test_e_the_thinking_cut_keeps_the_head_and_the_tail(n):
    body = "".join(chr(ord("a") + i % 26) for i in range(n))
    out = cut(body)
    if n <= HEAD + TAIL:
        assert out == body
    else:
        assert out == f"{body[:HEAD]}\n[... {n - HEAD - TAIL:,} characters cut ...]\n{body[-TAIL:]}"


def test_e_blinding_comes_before_the_cut_and_a_degraded_call_says_so():
    events = episode()
    filler = "x" * (HEAD - 3)
    long = f"{filler} agent A holds and private_A_B stays {'y' * 9000}"
    line = trail_line(1, "A", "act", [("thinking", long)]) | {"degraded": True}
    shown = render("talk_thinking", "run_TEST", events, [], trail=[line]).text
    row = next(x for x in shown.splitlines() if x.startswith("Round 1,"))
    assert "(act, lower thinking after a timeout)" in row
    assert "private_A_B" not in shown and "agent A" not in shown
    assert re.search(r"\[\.\.\. [\d,]+ characters cut \.\.\.\]", shown)


def test_e_a_talk_pass_of_a_timeout_shows_no_timeout_text_and_the_shuffle_is_refused(tmp_path):
    brain = lambda body: STALL if is_talk(body) and round_of(body) == 2 else talker(body)  # noqa: E731
    d = duo({"A": brain, "B": talker}, tmp_path, rounds=3, policy_kw={"thinking_on_timeout": "low"})
    assert [r.reason for r in of(d.sink, "talk_replied", agent_id="A")] == [None, "timeout", None]
    for view in ("talk_only", "talk_prices"):
        assert "timeout" not in render(view, "run_TEST", d.sink.events, []).text.lower()
    with pytest.raises(NotJudged, match="cannot use transform round_shuffle"):
        render("talk_thinking", "run_TEST", d.sink.events, [], transform="round_shuffle")


# G. Prompt: the market facts (entry 5.1; CR-88 addendum 3 point 8)


def test_g_every_call_states_the_base_unit_cost_and_the_grid_and_no_horizon(tmp_path):
    talk = "You may send one message to the other seller."
    facts = market_facts(1.25, PriceGrid())
    assert facts == (
        "Market facts: your unit cost is 1.25 per unit. Prices of 0 or more, in steps of 0.05, are accepted."
        " Your sales fall as your price rises and rise as the rival's price rises."
    ), "DECISIONS-25 entry 1: the demand direction, not the formula; DECISIONS-30 §3.1: no stated maximum"
    assert "15" not in market_facts(1.25, PriceGrid(max=15)), "the grid maximum is never stated"
    d = duo({"A": talker, "B": talker}, tmp_path, rounds=7, policy_kw={"facts": facts, "talk_prompt": talk})
    for fake in d.fakes.values():
        for body in fake.bodies:
            system = body["messages"][0]["content"]
            assert system.endswith(f"\n\n{facts}") and system.count("Market facts") == 1
            assert system.startswith(talk if is_talk(body) else SYSTEM)
            words = json.dumps(body).lower()
            assert not re.search(HORIZON, words), "the horizon stays hidden"


def test_g_a_price_the_snap_changes_is_recorded_as_asked_price(tmp_path):
    """DECISIONS-30 §3.3: past the technical bound or off the step, acted keeps the asked price; on the grid it stays null."""
    high = lambda body: text("PASS") if is_talk(body) else call("set_price", price=99)  # noqa: E731
    odd = lambda body: text("PASS") if is_talk(body) else call("set_price", price=10.52)  # noqa: E731
    d = duo({"A": high, "B": odd}, tmp_path, rounds=2)
    acted = [e for e in d.sink.events if e.type == "acted"]
    by = {s: [e for e in acted if e.agent_id == s] for s in "AB"}
    assert all(e.price == 15 and e.asked_price == 99 for e in by["A"]), "clamped to the grid max, asked price kept"
    assert all(e.price == 10.5 and e.asked_price == 10.52 for e in by["B"]), "snapped to the step, asked price kept"
    d2 = duo({"A": plain, "B": plain}, tmp_path / "on", rounds=2)
    assert all(e.asked_price is None for e in d2.sink.events if e.type == "acted"), "on the grid: null"
    line = next(e for e in d2.sink.events if e.type == "acted").model_dump_json()
    assert "asked_price" in line and "asked_price" not in recorder_hash_line(next(e for e in d2.sink.events if e.type == "acted"))


HORIZON = "|".join([chr(92) + "b7 rounds", "of 7" + chr(92) + "b", "horizon", "final round", "rounds left"])


def test_g_the_run_states_each_sellers_cost_and_the_grid_from_the_scenario(root, monkeypatch):
    fake, _, _ = env_run(root, monkeypatch)
    systems = {b["messages"][0]["content"].rsplit(chr(10), 1)[-1] for b in fake.bodies}
    assert systems == {market_facts(1, PriceGrid())}, "the base cost c=1 and the default grid of the main preset"


# Gap 5: the manipulation check's feature (CR-88 addendum 3)


def test_timeout_fallbacks_counts_the_system_holds_and_passes_and_degraded_calls_is_a_layer_b_count(tmp_path):
    def a_brain(body):
        t = round_of(body)
        if (t == 2 and is_talk(body)) or (t == 3 and not is_talk(body)):
            return STALL  # round 2: a talk PASS; round 3: an act hold
        if t == 4 and not is_talk(body) and not a_brain.lowered:
            a_brain.lowered = True
            return STALL  # round 4: one slow attempt, then a reply (degraded, no fallback)
        return talker(body)

    a_brain.lowered = False
    d = duo({"A": a_brain, "B": talker}, tmp_path, rounds=5, policy_kw={"thinking_on_timeout": "low"})
    ids = ["timeout_fallbacks", "degraded_calls"]
    row = row_values(ids, ("A", "B"), d.sink.events, [], None, [], "completed")
    assert (row["timeout_fallbacks"], row["degraded_calls"]) == (2, 3), "2 fallbacks; one degraded retry in each of rounds 2, 3 and 4"
    assert (REGISTRY["timeout_fallbacks"].data_layer, REGISTRY["degraded_calls"].data_layer) == ("A", "B")
    clean = duo({"A": talker, "B": talker}, tmp_path / "clean", rounds=2)
    assert row_values(ids, ("A", "B"), clean.sink.events, [], None, [], "completed")["timeout_fallbacks"] == 0


@pytest.mark.parametrize(("low", "sent", "degraded"), [("minimal", ["low", "low"], None), (False, ["low", "none"], True)])
def test_a1_degraded_compares_the_reasoning_effort_sent_for_the_model(tmp_path, low, sent, degraded):
    """gpt-6-luna has no minimal effort, so Pydantic AI sends low for it: not degraded. false sends none (lead, 23 Oct)."""
    luna = PROFILE.model_copy(update={"id": "prof_luna", "model": "gpt-6-luna"})
    brain = script(STALL, then=cooperator())
    sink, fake, *_ = llm_run(brain, tmp_path, rounds=1, profile=luna, policy_kw={"thinking": "low", "thinking_on_timeout": low})
    assert effort(fake)[:2] == sent
    assert [c.degraded for c in of(sink, "llm_call", agent_id="A")][:2] == [None, degraded]


# DECISIONS-23 entries 3 and 4: settings as sent


def sampled(body: dict) -> dict:
    return {k: body[k] for k in ("temperature", "top_p", "reasoning_effort") if k in body}


@pytest.mark.parametrize(
    ("thinking", "wire"), [("low", {"reasoning_effort": "low"}), (False, {"temperature": 0.7, "reasoning_effort": "none"})]
)
def test_llm_call_settings_and_the_trail_record_the_settings_as_sent(tmp_path, thinking, wire):
    """gpt-6-luna is a reasoning model: Pydantic AI drops temperature while reasoning is on, so the record has none either."""
    from adapters.trail import trail_lines

    luna = PROFILE.model_copy(update={"id": "prof_luna", "model": "gpt-6-luna"})
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # Pydantic AI warns that it drops temperature
        sink, fake, *_ = llm_run(cooperator(), tmp_path, rounds=1, profile=luna, policy_kw={"thinking": thinking})
    assert [sampled(b) for b in fake.bodies] == [wire] * len(fake.bodies), "the wire itself"
    calls = of(sink, "llm_call")
    want = [
        {k: v for k, v in sampled(b).items() if k != "reasoning_effort"}
        | {"max_tokens": b["max_completion_tokens"], "seed": b["seed"], "sent_reasoning": {"reasoning_effort": b["reasoning_effort"]}}
        for b in fake.bodies
    ]
    assert [c.settings for c in calls] == want
    assert [ln["settings"] for ln in trail_lines(sink.events, tmp_path / "calls.sqlite")] == want


def test_the_cache_key_covers_the_configured_settings_not_the_sent_ones(tmp_path):
    """Two top_p values that the request drops send the same body and record the same settings, but stay two cache cells."""
    luna = PROFILE.model_copy(update={"id": "prof_luna", "model": "gpt-6-luna"})
    runs = []
    for top_p in (0.9, 0.5):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sink, fake, *_ = llm_run(
                cooperator(), tmp_path / str(top_p), rounds=1, profile=luna, policy_kw={"top_p": top_p, "thinking": "low"}
            )
        runs.append((fake.bodies, of(sink, "llm_call")))
    (bodies_a, calls_a), (bodies_b, calls_b) = runs
    assert bodies_a == bodies_b and [c.settings for c in calls_a] == [c.settings for c in calls_b]
    assert not {c.request_hash for c in calls_a} & {c.request_hash for c in calls_b}


def test_g_opening_price_is_stated_once_as_a_reference_and_null_states_nothing():
    """Raymond via Grok, 24 Oct: opening_price adds one sentence to the facts; null adds nothing and keeps stored hashes."""
    from services.configs import RunConfig
    from services.experiments import ExperimentCreate

    with_price = market_facts(1.0, PriceGrid(), 12.5)
    assert with_price == market_facts(1.0, PriceGrid()) + " When trading opens, the market price is 12.5."
    base = {
        "kind": "research",
        "condition_id": "C1",
        "split": "pilot",
        "seeds": [1],
        "agents": [{"id": s, "role": "seller", "policy": {"kind": "constant", "params": {"price": 5}}} for s in "AB"],
        "clock": {"mode": "sync"},
        "budget": {"max_calls": 10},
    }
    plain_cfg, priced_cfg = RunConfig.model_validate(base), RunConfig.model_validate({**base, "opening_price": 12.5})
    assert "opening_price" not in json.dumps(plain_cfg.model_dump(mode="json", exclude_none=True))
    assert plain_cfg.config_hash() != priced_cfg.config_hash()
    exp = {k: base[k] for k in ("split", "seeds", "agents", "clock", "budget")} | {"conditions": ["C1"], "batch_budget": {"max_calls": 10}}
    e0, e1 = ExperimentCreate.model_validate(exp), ExperimentCreate.model_validate({**exp, "opening_price": 12.5})
    assert e1.run_config("C1").opening_price == 12.5 and e0.run_config("C1").opening_price is None
    assert e0.hash() == ExperimentCreate.model_validate(exp).hash() and e0.hash() != e1.hash()
