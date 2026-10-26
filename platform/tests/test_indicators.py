import ast
import random
from pathlib import Path

import pytest

import indicators
from indicators import REGISTRY, Indicator, LayerError, OutputSpec, SeriesView, instance
from mockgen.__main__ import generate
from simkit import JOINT, NASH, capability, run
from simschema.episode import read_episode


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    out = tmp_path_factory.mktemp("mock")
    generate(out)
    _, events = read_episode((out / "episode-demo.jsonl").read_text(encoding="utf-8").splitlines())
    return SeriesView.from_events(events)


@pytest.fixture(scope="module")
def noisy():
    rng = random.Random(3)
    walk = {s: {t: round(rng.uniform(5.75, 10.5) * 20) / 20 for t in range(1, 31)} for s in "AB"}
    sink, *_ = run({s: ("price_schedule", {"prices": walk[s], "default": NASH}) for s in "AB"})
    return SeriesView.from_events(sink.events)


def test_the_first_set_is_registered():
    assert {"delta_p", "br_gap_net", "corr_raw", "corr_cond", "corr_lag", "msg_volume", "query_rate"} <= set(REGISTRY.ids())


@pytest.mark.parametrize("ind_id", sorted(REGISTRY.ids()))
@pytest.mark.parametrize("cut", [3, 9, 17, 29])
@pytest.mark.parametrize("episode", ["demo", "noisy"])
def test_no_indicator_looks_ahead(request, episode, ind_id, cut):
    view = request.getfixturevalue(episode)
    full = instance(view, ind_id, {})
    part = instance(view.upto(cut), ind_id, {})
    assert part.t == full.t[: len(part.t)]
    for name, values in part.outputs.items():
        assert values == full.outputs[name][: len(values)], name


@pytest.mark.parametrize("ind_id", sorted(REGISTRY.ids()))
def test_every_indicator_publishes_a_flat_params_form_with_defaults_and_titles(ind_id):
    spec = REGISTRY[ind_id].spec()
    assert spec["data_layer"] in ("A", "B") and spec["pane"] in ("overlay", "separate")
    for prop in spec["params_schema"]["properties"].values():
        assert prop["type"] in ("integer", "number", "boolean", "string") and "default" in prop and "title" in prop


def test_delta_p_is_zero_at_nash_and_one_at_the_joint_optimum():
    sink, *_ = run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": JOINT})})
    out = instance(SeriesView.from_events(sink.events), "delta_p", {}).outputs
    assert set(out["delta_p.A"]) == {0.0} and set(out["delta_p.B"]) == {1.0} and set(out["delta_p"]) == {0.5}


def test_net_br_gap_is_zero_for_a_best_responder_and_positive_for_a_cooperator():
    sink, *_ = run({"A": ("best_response", {}), "B": ("constant", {"price": JOINT})})
    out = instance(SeriesView.from_events(sink.events), "br_gap_net", {}).outputs
    assert out["br_gap_net.A"][0] is None and set(out["br_gap_net.A"][2:]) == {0.0}
    br_to_a = 6.30  # (a + d·7.35 + b·c) / 2b on the 0.05 grid; A priced 7.35 in round 2
    assert out["br_gap_net.B"][2] == pytest.approx(JOINT - br_to_a)
    shifted = instance(SeriesView.from_events(sink.events), "br_gap_net", {"baseline": 0.5}).outputs
    assert shifted["br_gap_net.B"][2] == pytest.approx(JOINT - br_to_a - 0.5)


def test_raw_correlation_is_one_when_both_sellers_move_together():
    moves = {10: 6.0, 11: 6.5, 12: 6.2, 13: 7.0, 14: 6.4, 15: 6.9, 16: 6.1, 17: 6.6, 18: 7.2, 19: 6.3}
    sink, *_ = run(
        {"A": ("price_schedule", {"prices": moves, "default": NASH}), "B": ("price_schedule", {"prices": moves, "default": NASH})}
    )
    out = instance(SeriesView.from_events(sink.events), "corr_raw", {"window": 8}).outputs["corr_raw.A_B"]
    assert all(v is None for v in out[:8]) and out[18] == pytest.approx(1.0)


def test_lagged_correlation_finds_a_follower():
    moves = {10: 6.0, 11: 6.5, 12: 6.2, 13: 7.0, 14: 6.4, 15: 6.9, 16: 6.1, 17: 6.6, 18: 7.2, 19: 6.3}
    follow = {t + 1: p for t, p in moves.items()}
    sink, *_ = run(
        {"A": ("price_schedule", {"prices": follow, "default": NASH}), "B": ("price_schedule", {"prices": moves, "default": NASH})}
    )
    out = instance(SeriesView.from_events(sink.events), "corr_lag", {"window": 8}).outputs
    assert out["corr_lag.A_B"][19] == pytest.approx(1.0)


def test_message_and_query_counts(demo):
    msgs = instance(demo, "msg_volume", {}).outputs
    assert set(msgs["msg_volume.A"]) == {1.0} and set(msgs["msg_volume.B"]) == {0.0}
    sink, *_ = run(
        {"A": ("grim_trigger", {"target": JOINT}), "B": ("constant", {"price": JOINT})},
        caps={"A": capability(), "B": capability(see_rivals=False)},
    )
    q = instance(SeriesView.from_events(sink.events), "query_rate", {}).outputs
    assert q["query_rate.A"][1] >= 1 and set(q["query_rate.B"]) == {0.0}


def test_bad_params_are_refused_with_a_path():
    with pytest.raises(indicators.ParamsError) as e:
        instance(SeriesView(t=(1,), sellers=("A", "B"), data={}), "corr_raw", {"window": 1})
    assert e.value.errors[0]["path"] == "window"


def test_a_layer_a_indicator_cannot_declare_a_layer_b_input_or_any_layer_c_input():
    class Cheat(Indicator):
        id, version, name, description, data_layer, pane = "cheat", "1", "Cheat", "x", "A", "separate"
        inputs = ("price.*", "nash.*")
        outputs = (OutputSpec(name="x", kind="line", unit="u"),)

    class Mind(Indicator):
        id, version, name, description, data_layer, pane = "mind", "1", "Mind", "x", "B", "separate"
        inputs = ("plan.*",)
        outputs = (OutputSpec(name="x", kind="line", unit="u"),)

    for cls in (Cheat, Mind):
        with pytest.raises(LayerError):
            indicators.IndicatorRegistry().register(cls)


def test_an_indicator_cannot_read_a_series_it_did_not_declare(demo):
    view = demo.restrict(("price.*",))
    assert view["price.A"]
    with pytest.raises(LayerError):
        view["nash.A"]


def test_the_indicators_package_imports_only_the_schema_package():
    root = Path(indicators.__file__).parent
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            assert not any(n.split(".")[0] in ("simcore", "api", "adapters", "bench") for n in names), (path, names)
