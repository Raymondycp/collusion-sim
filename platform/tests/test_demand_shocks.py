import hashlib
import json

import pytest

from simcore.demand import DEMAND, DemandModelSpec, DemandRegistry
from simcore.demand.builtin.linear import LINEAR, LinearDemand, LinearParams, LinearTargets
from simcore.market import Market, PriceGrid
from simcore.scenario import DemandRef, Event, Horizon, MarketSpec, Scenario, ScenarioHashError
from simcore.shocks import Shock, path_in_schema

BASE = {"a": 10, "b": 1.5, "d": 1.0, "c": 1.0}


def scenario(events, **demand):
    return Scenario(
        id="scn_t",
        name="t",
        market=MarketSpec(demand=DemandRef(id="linear", params={**BASE, **demand})),
        n_sellers=2,
        horizon=Horizon(min_rounds=30, max_rounds=30, stop_prob=0),
        events=events,
    )


def checks(s):
    return {c.id: c for c in s.check().checks}


def test_linear_is_registered_with_all_references():
    spec = DEMAND.get("linear")
    assert spec is LINEAR and spec.version == "1" and spec.research_allowed
    assert set(spec.params_schema()["properties"]) == {"a", "b", "d", "c", "c_offset"}
    with pytest.raises(KeyError):
        DEMAND.get("linear", "9")
    with pytest.raises(KeyError):
        DEMAND.get("logit")


def test_a_model_without_closed_forms_is_exploratory_only():
    reg = DemandRegistry()
    spec = reg.register(
        DemandModelSpec(id="agent", version="1", name="Buyer agents", formula="", params_model=LinearParams, factory=LinearDemand)
    )
    assert not spec.research_allowed
    with pytest.raises(ValueError):
        reg.register(spec)


@pytest.mark.parametrize(
    "targets, expect",
    [
        ({"nash": 5.75, "joint_optimum": 10.5, "b": 1.5, "c": 1.0}, {"a": 10, "d": 1.0}),
        ({"nash": 6.75, "joint_optimum": 12.5, "b": 1.5, "c": 1.0}, {"a": 12, "d": 1.0}),
    ],
)
def test_solver_turns_target_prices_into_params_with_those_references(targets, expect):
    params = LINEAR.solver.solve(LinearTargets(**targets))
    assert params.a == pytest.approx(expect["a"]) and params.d == pytest.approx(expect["d"])
    ref = Market(LinearDemand(), PriceGrid()).reference(params, ("A", "B", "C"))
    assert ref.nash == pytest.approx(targets["nash"]) and ref.joint_optimum == pytest.approx(targets["joint_optimum"])


def test_solver_rejects_targets_out_of_order():
    with pytest.raises(ValueError):
        LinearTargets(nash=10.5, joint_optimum=5.75)


def test_shock_ops_and_a_missing_offset_start_from_zero():
    p = {"a": 10, "c": 1.0, "c_offset": {}}
    assert Shock(path="/a", op="add", value=2).apply(p)["a"] == 12
    assert Shock(path="/a", op="mul", value=1.5).apply(p)["a"] == 15
    assert Shock(path="/c", op="set", value=3).apply(p)["c"] == 3
    assert Shock(path="/c_offset/B", op="add", value=0.5).apply(p)["c_offset"] == {"B": 0.5}
    assert p == {"a": 10, "c": 1.0, "c_offset": {}}
    with pytest.raises(ValueError):
        Shock(path="/a/x", op="add", value=1).apply(p)


def test_path_in_schema_follows_properties_and_maps():
    schema = LINEAR.params_schema()
    assert path_in_schema(schema, "/a") and path_in_schema(schema, "/c_offset/A")
    assert not path_in_schema(schema, "/e") and not path_in_schema(schema, "/c_offset")


def test_shocks_fold_in_event_order_and_cost_shock_moves_every_seller():
    up = Event(announce=3, effect=3, length=5, shocks=[Shock(path="/c", op="add", value=0.5)], visible=["A", "B"], news="Costs rose.")
    firm = Event(
        announce=4, effect=4, length=2, shocks=[Shock(path="/c_offset/B", op="add", value=0.5)], visible=["B"], news="B's costs rose."
    )
    s = scenario([up, firm], c_offset={"A": 0.2})
    p = s.params(4)
    assert (p.cost_of("A"), p.cost_of("B")) == pytest.approx((1.7, 2.0))
    assert s.params(2).cost_of("A") == pytest.approx(1.2)
    assert Market(LinearDemand(), PriceGrid()).reference(s.params(3).model_copy(update={"c_offset": {}}), ("A", "B")).nash == pytest.approx(
        6.125
    )


def test_check_rejects_unknown_path_and_invalid_result():
    bad_path = Event(announce=3, effect=3, length=2, shocks=[Shock(path="/e", op="add", value=1)], visible=["A"], news="Demand rose.")
    assert checks(scenario([bad_path]))["shock_paths"].ok is False
    unstable = Event(announce=3, effect=3, length=2, shocks=[Shock(path="/d", op="set", value=2.0)], visible=["A"], news="Rival pull rose.")
    report = checks(scenario([unstable]))
    assert report["stability"].ok is False and "Round 3" in report["stability"].message
    negative = Event(announce=3, effect=3, length=2, shocks=[Shock(path="/c", op="add", value=-5)], visible=["A"], news="Costs fell.")
    assert "Round 3" in checks(scenario([negative]))["stability"].message


def test_news_direction_uses_the_shock_sign_unless_marked_misleading():
    shock = [Shock(path="/a", op="mul", value=0.8)]
    honest = Event(announce=3, effect=3, length=2, shocks=shock, visible=["A"], news="Demand fell.")
    lie = honest.model_copy(update={"news": "Demand will rise."})
    assert checks(scenario([honest]))["news_direction"].ok
    assert checks(scenario([lie]))["news_direction"].ok is False
    assert checks(scenario([lie.model_copy(update={"misleading": True})]))["news_direction"].ok
    mixed = Event(
        announce=3, effect=3, length=2, shocks=[*shock, Shock(path="/c", op="add", value=0.5)], visible=["A"], news="Demand fell."
    )
    assert "mixed" in checks(scenario([mixed]))["news_direction"].message


def test_event_kind_follows_from_shocks_and_must_match_them():
    news = Event(announce=3, visible=["A"], news="Analysts expect a normal season.")
    shock = Event(announce=3, effect=4, length=2, shocks=[Shock(path="/c", op="add", value=0.5)], visible=["A"], news="Costs will rise.")
    assert (news.kind, shock.kind) == ("news_only", "param_shock")
    with pytest.raises(ValueError, match="news-only"):
        Event(kind="news_only", announce=3, effect=4, length=2, shocks=shock.shocks, visible=["A"])
    with pytest.raises(ValueError):
        Event(kind="grid_change", announce=3, visible=["A"])


def _legacy_file(tmp_path, tamper=False):
    content = {
        "id": "scn_old",
        "name": "old",
        "description": "",
        "n_sellers": 2,
        "seller_ids": ["A", "B"],
        "market": {
            "type": "linear",
            "params": {"a": 10.0, "b": 1.5, "d": 1.0, "c": 1.0, "c_by_seller": {"B": 1.2}},
            "grid": {"min": 0.0, "max": 15.0, "step": 0.05},
            "demand_noise_std": 0,
        },
        "horizon": {"min_rounds": 30, "max_rounds": 30, "stop_prob": 0.0},
        "quiet_window": [18, 25],
        "events": [
            {
                "announce": 6,
                "effect": 6,
                "length": 4,
                "param": "c",
                "seller": None,
                "delta": 0.5,
                "visible": ["A", "B"],
                "news": "Oil prices rose.",
                "news_source": "human",
            },
            {
                "announce": 8,
                "effect": 8,
                "length": 2,
                "param": "c_i",
                "seller": "A",
                "delta": 0.3,
                "visible": ["A"],
                "news": "A's supplier raised prices.",
                "news_source": "human",
            },
        ],
    }
    old_hash = "sha256:" + hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if tamper:
        content["events"][0]["delta"] = 0.6
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({**content, "status": "frozen", "hash": old_hash, "approved_by": "x", "frozen_at": "2026-10-20T00:00:00+00:00"})
    )
    return path, old_hash


def test_legacy_scenario_file_loads_through_the_converter_with_the_same_market(tmp_path):
    path, old_hash = _legacy_file(tmp_path)
    s = Scenario.load(path)
    assert s.provenance == {"converted_from_hash": old_hash} and s.hash != old_hash
    assert s.market.demand.params["c_offset"] == pytest.approx({"B": 0.2})
    p7, p8 = s.params(7), s.params(8)
    assert (p7.cost_of("A"), p7.cost_of("B")) == pytest.approx((1.5, 1.7))
    assert (p8.cost_of("A"), p8.cost_of("B")) == pytest.approx((1.8, 1.7))


def test_tampered_legacy_file_is_rejected(tmp_path):
    path, _ = _legacy_file(tmp_path, tamper=True)
    with pytest.raises(ScenarioHashError):
        Scenario.load(path)
