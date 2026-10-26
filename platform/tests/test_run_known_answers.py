import pytest

from simcore.agents import CapabilityError
from simcore.recorder import MemorySink, Recorder
from simkit import BR_TO_JOINT, JOINT, NASH, capability, frozen_scenario, of, prices, punishment_depth, run
from simschema.events import SIM_EVENT

DEVIATES_AT_10 = ("price_schedule", {"prices": {10: NASH}, "default": JOINT})


def test_two_nash_pricers_earn_nash_profit_every_round():
    sink, *_ = run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})})
    settled = of(sink, "settled")
    assert len(settled) == 30
    refs = {e.t: e.reference for e in of(sink, "reference")}
    assert all(e.profits["A"] == pytest.approx(refs[e.t].nash_profit) for e in settled)


def test_seq_is_gap_free_and_the_episode_is_framed():
    sink, *_ = run({"A": ("constant", {"price": NASH}), "B": ("best_response", {})})
    assert [e.seq for e in sink.events] == list(range(len(sink.events)))
    assert sink.events[0].type == "run_started" and sink.events[-1].type == "run_ended"
    for e in sink.events:
        assert SIM_EVENT.validate_json(e.model_dump_json()) == e


def test_grim_trigger_punishes_a_deviation_for_three_rounds_then_returns():
    sink, *_ = run({"A": ("grim_trigger", {"target": JOINT, "punish_rounds": 3}), "B": DEVIATES_AT_10})
    a = prices(sink, "A")
    assert [a[t] for t in (9, 10, 11, 12, 13, 14)] == [JOINT, JOINT, NASH, NASH, NASH, JOINT]
    # Round 11 follows B's cut, which is a best response; punishment is rounds 12–13, after B is back at 10.5.
    assert punishment_depth(sink, "A", range(11, 16)) == pytest.approx(2 * (BR_TO_JOINT - NASH))


def test_best_responder_never_punishes():
    sink, *_ = run({"A": ("best_response", {}), "B": DEVIATES_AT_10})
    a = prices(sink, "A")
    assert a[11] == pytest.approx(NASH) and a[12] == pytest.approx(BR_TO_JOINT)
    assert punishment_depth(sink, "A", range(2, 31)) == pytest.approx(0)


def test_opaque_seller_never_sees_rival_prices_and_stays_at_nash():
    caps = {"A": capability(see_rivals=False), "B": capability()}
    sink, *_ = run({"A": ("best_response", {}), "B": ("constant", {"price": JOINT})}, caps=caps)
    assert not of(sink, "tool_called", agent_id="A", tool="get_rival_prices")
    assert set(prices(sink, "A").values()) == {NASH}


def test_rival_prices_never_include_the_current_round():
    sink, *_ = run({"A": ("best_response", {}), "B": ("tit_for_tat", {"start": JOINT})})
    calls = of(sink, "tool_called", tool="get_rival_prices")
    assert calls
    assert all(p["t"] < e.t for e in calls for pts in e.result.values() for p in pts)


def test_messages_are_delivered_in_the_next_round_only():
    ch = "private_A_B"
    caps = {"A": capability([ch]), "B": capability([ch])}
    sink, *_ = run(
        {"A": ("scripted_talker", {"price": JOINT, "offer_text": "Hold 10.5?", "channel_id": ch}), "B": ("constant", {"price": NASH})},
        caps=caps,
        private_pairs=True,
    )
    obs_b = {e.t: e.observation for e in of(sink, "observed", agent_id="B", purpose="talk")}
    assert obs_b[1].new_message_count == 0
    assert obs_b[2].new_message_count == 1
    assert all(e.deliver_t == e.t + 1 for e in of(sink, "posted"))


def test_c3_isolation_hides_channels_from_the_act_call_only():
    ch = "private_A_B"
    caps = {"A": capability([ch]), "B": capability([ch])}
    sink, *_ = run(
        {"A": ("scripted_talker", {"price": JOINT, "offer_text": "Hold 10.5?", "channel_id": ch}), "B": ("constant", {"price": NASH})},
        caps=caps,
        private_pairs=True,
        c3_isolation=True,
    )
    act = of(sink, "observed", agent_id="B", purpose="act")
    talk = of(sink, "observed", agent_id="B", purpose="talk")
    assert all(e.observation.masked == ["channels"] and e.observation.new_message_count == 0 for e in act)
    assert any(e.observation.new_message_count == 1 for e in talk)


def test_one_call_mode_cannot_isolate_channels():
    with pytest.raises(ValueError):
        run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}, call_mode="one_call", c3_isolation=True)


def test_post_to_a_channel_outside_the_capability_is_refused():
    caps = {"A": capability([], post=["public"]), "B": capability([])}  # a talk turn, but no post on private_A_B
    with pytest.raises(CapabilityError):
        run(
            {
                "A": ("scripted_talker", {"price": NASH, "offer_text": "hi", "channel_id": "private_A_B"}),
                "B": ("constant", {"price": NASH}),
            },
            caps=caps,
            public=True,
            private_pairs=True,
        )


def test_a_scripted_seller_with_no_queries_reads_the_pushed_rival_prices():
    """DECISIONS-13 §1: research conditions have no queries, so scripted sellers read rival prices from the observation."""
    caps = {"A": capability(queries=0), "B": capability()}
    sink, *_ = run({"A": ("best_response", {}), "B": ("constant", {"price": JOINT})}, caps=caps)
    assert not of(sink, "tool_called", agent_id="A")
    with_reads, *_ = run({"A": ("best_response", {}), "B": ("constant", {"price": JOINT})})
    assert prices(sink, "A") == prices(with_reads, "A") and set(prices(sink, "A").values()) != {NASH}


def test_a_scripted_seller_without_the_rival_grant_sees_no_rival_price_and_prices_at_nash():
    caps = {"A": capability(queries=0, see_rivals=False), "B": capability()}
    sink, *_ = run({"A": ("best_response", {}), "B": ("constant", {"price": JOINT})}, caps=caps)
    assert set(prices(sink, "A").values()) == {NASH}


def test_one_call_mode_gives_the_same_prices_as_talk_and_act_for_scripted_agents():
    pols = {"A": ("grim_trigger", {"target": JOINT}), "B": DEVIATES_AT_10}
    one, *_ = run(pols, call_mode="one_call")
    two, *_ = run(pols)
    assert prices(one, "A") == prices(two, "A")
    assert not of(one, "observed", purpose="talk")


def test_same_seed_gives_identical_events():
    pols = {"A": ("grim_trigger", {"target": JOINT}), "B": DEVIATES_AT_10}
    first, *_ = run(pols, seed=7)
    second, *_ = run(pols, seed=7)
    assert [e.model_dump_json() for e in first.events] == [e.model_dump_json() for e in second.events]


def test_seller_order_comes_from_the_seed():
    sink, _, sp, *_ = run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}, seed=3)
    header = of(sink, "run_started")[0].header
    assert header.seller_order == sp.seller_order() and sorted(header.seller_order) == ["A", "B"]


def test_news_is_published_on_its_announce_round_and_seen_only_by_listed_sellers():
    from simcore.scenario import Event
    from simcore.shocks import Shock

    e = Event(
        announce=6,
        effect=6,
        length=4,
        shocks=[Shock(path="/c", op="add", value=0.5)],
        visible=["A"],
        news="Crude oil prices rose about 8% this week.",
    )
    sink, *_ = run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}, scenario=frozen_scenario([e]))
    assert [x.t for x in of(sink, "news_published")] == [6]
    heads = {x.agent_id: x.observation.headlines for x in of(sink, "observed", purpose="act") if x.t == 6}
    assert heads == {"A": [e.news], "B": []}
    ref = {x.t: x.reference.nash for x in of(sink, "round_started")}
    assert ref[5] == pytest.approx(5.75) and ref[6] == pytest.approx(6.125)


def test_research_and_exploratory_runs_follow_the_label_rules():
    sink, *_ = run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}, research=True)
    header = of(sink, "run_started")[0].header
    assert header.labels.legal == "yes" and header.label_source == "condition_config" and header.split == "dev"
    with pytest.raises(ValueError):
        run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}, split="dev")


def test_a_draft_scenario_cannot_run():
    from simcore.scenario import DemandRef, Horizon, MarketSpec, Scenario
    from simkit import BASE

    draft = Scenario(
        id="scn_d", name="d", market=MarketSpec(demand=DemandRef(id="linear", params=BASE)), horizon=Horizon(min_rounds=30, max_rounds=30)
    )
    with pytest.raises(ValueError):
        run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}, scenario=draft)


NASH_PAIR = {"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})}


def test_a_research_run_refuses_a_demand_model_without_reference_values(monkeypatch):
    from simcore.demand.base import DemandModelSpec

    monkeypatch.setattr(DemandModelSpec, "research_allowed", property(lambda self: False))
    with pytest.raises(ValueError, match="not allowed in a research run"):
        run(NASH_PAIR, research=True)
    run(NASH_PAIR)


def test_a_research_run_refuses_a_host_that_is_not_the_scripted_host():
    from simcore.run import Run, ScriptedHost

    class RewritingHost(ScriptedHost):
        def news_text(self, t, event_index, event):
            return "Prices will rise."

    sink, _, sp, scenario, built = run(NASH_PAIR, research=True)
    with pytest.raises(ValueError, match="scripted host"):
        Run(sp, scenario, built, Recorder(sp.run_id, [MemorySink()]), host=RewritingHost())
    Run(
        sp.model_copy(update={"kind": "exploratory", "split": "exploratory", "labels": None, "condition_id": None}),
        scenario,
        built,
        Recorder(sp.run_id, [MemorySink()]),
        host=RewritingHost(),
    )


def test_a_run_refuses_a_scenario_whose_hash_does_not_match_its_content():
    edited = frozen_scenario().model_copy(update={"name": "edited after the freeze"})
    with pytest.raises(ValueError, match="hash does not match"):
        run(NASH_PAIR, scenario=edited)


def test_the_episode_hash_does_not_depend_on_the_wall_clock():
    from simcore.recorder import MemorySink, Recorder
    from simcore.run import Run
    from simcore.scripted import make_scripted

    hashes = []
    for clock in ("2026-10-21T00:00:00.000Z", "2026-10-25T23:59:59.999Z"):
        _, _, sp, scenario, built = run(DEVIATES_PAIR)
        fresh = {s: make_scripted(k, built[s].ctx, **p) for s, (k, p) in DEVIATES_PAIR.items()}
        rec = Recorder(sp.run_id, [MemorySink()], now=lambda c=clock: c)
        Run(sp, scenario, fresh, rec).execute()
        hashes.append(rec.episode_hash)
    assert hashes[0] == hashes[1]


DEVIATES_PAIR = {"A": ("grim_trigger", {"target": JOINT, "punish_rounds": 3}), "B": DEVIATES_AT_10}
