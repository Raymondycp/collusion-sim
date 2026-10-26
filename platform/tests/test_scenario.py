import json

import pytest

from simcore.scenario import DemandRef, Event, Horizon, MarketSpec, Scenario, ScenarioHashError
from simcore.shocks import Shock

BASE = {"a": 10, "b": 1.5, "d": 1.0, "c": 1.0}


def linear(**params):
    return MarketSpec(demand=DemandRef(id="linear", params={**BASE, **params}))


def scenario(events, **kw):
    return Scenario(
        id="scn_test",
        name="test",
        market=linear(),
        n_sellers=2,
        horizon=Horizon(min_rounds=30, max_rounds=40, stop_prob=0.1),
        events=events,
        **kw,
    )


COST = Event(
    announce=6,
    effect=6,
    length=4,
    shocks=[Shock(path="/c", op="add", value=0.5)],
    visible=["A", "B"],
    news="Crude oil prices rose about 8% this week.",
)
DEMAND = Event(
    announce=12,
    effect=14,
    length=9999,
    shocks=[Shock(path="/a", op="add", value=2.0)],
    visible=["A"],
    news="Demand will rise: a holiday starts in two weeks.",
)
PLACEBO = Event(announce=28, visible=["A", "B"], news="Analysts expect busy weeks ahead.")


def test_params_follow_event_effect_window():
    s = scenario([COST, DEMAND, PLACEBO])
    assert s.params(5).c == 1.0
    assert [s.params(t).c for t in (6, 7, 8, 9)] == [1.5] * 4
    assert s.params(10).c == 1.0
    assert s.params(13).a == 10
    assert s.params(14).a == 12
    assert s.params(40).a == 12


def test_firm_cost_shock_changes_only_that_seller():
    e = Event(
        announce=3,
        effect=3,
        length=2,
        shocks=[Shock(path="/c_offset/B", op="add", value=0.5)],
        visible=["B"],
        news="Supplier of B raised prices.",
    )
    p = scenario([e]).params(3)
    assert p.cost_of("A") == 1.0
    assert p.cost_of("B") == 1.5


def test_announced_news_respects_visibility():
    s = scenario([COST, DEMAND, PLACEBO])
    assert [e.news for e in s.announced(12, "A")] == [DEMAND.news]
    assert s.announced(12, "B") == []
    assert s.announced(6, "B") == [COST]


def test_hash_ignores_key_order_and_detects_tampering(tmp_path):
    s = scenario([COST]).frozen(approved_by="test")
    path = tmp_path / "s.json"
    s.write(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(dict(reversed(list(data.items())))), encoding="utf-8")
    assert Scenario.load(path).hash == s.hash
    data["events"][0]["shocks"][0]["value"] = 0.6
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ScenarioHashError):
        Scenario.load(path)


def test_draft_cannot_be_loaded_as_frozen(tmp_path):
    path = tmp_path / "d.json"
    scenario([COST]).write(path)
    with pytest.raises(ScenarioHashError):
        Scenario.load(path)


def check_ids(report):
    return {c.id: c.ok for c in report.checks}


def test_checks_pass_for_a_valid_scenario():
    report = scenario([COST, DEMAND, PLACEBO]).check()
    assert report.ok, report


def test_check_flags_an_effect_inside_the_quiet_window():
    e = COST.model_copy(update={"announce": 19, "effect": 20})
    assert check_ids(scenario([e]).check())["quiet_window"] is False


def test_check_flags_news_whose_direction_contradicts_the_delta():
    e = COST.model_copy(update={"news": "Crude oil prices fell sharply this week."})
    assert check_ids(scenario([e]).check())["news_direction"] is False


def test_news_only_event_cannot_carry_a_parameter_change():
    with pytest.raises(ValueError):
        Event(kind="news_only", announce=28, visible=["A"], news="Rumour.", shocks=[Shock(path="/a", op="add", value=1.0)])
    with pytest.raises(ValueError):
        Event(announce=6, shocks=[Shock(path="/c", op="add", value=0.5)], visible=["A"], news="Costs rose.")


def test_unknown_event_kind_is_rejected():
    with pytest.raises(ValueError):
        Event(kind="grid_change", announce=3, visible=["B"], news="New grid.")


def test_check_flags_unknown_visible_seller():
    e = COST.model_copy(update={"visible": ["A", "Z"]})
    assert check_ids(scenario([e]).check())["visible_ids"] is False


def test_check_flags_cross_slope_not_below_own_slope():
    s = scenario([]).model_copy(update={"market": linear(b=1.0)})
    assert check_ids(s.check())["stability"] is False


def test_horizon_stop_round_is_paired_by_seed():
    s = scenario([])
    assert s.horizon.stop_round(7) == s.horizon.stop_round(7)
    rounds = {s.horizon.stop_round(seed) for seed in range(200)}
    assert min(rounds) >= 30 and max(rounds) <= 40 and len(rounds) > 3
