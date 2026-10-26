import pytest

from simcore.demand.builtin.linear import LinearDemand, LinearParams
from simcore.market import Market, PriceGrid

BASE = LinearParams(a=10, b=1.5, d=1.0, c=1.0)
COST_15 = BASE.model_copy(update={"c": 1.5})
AB = ("A", "B")


def market(step=0.05):
    return Market(LinearDemand(), PriceGrid(min=0, max=15, step=step))


@pytest.mark.parametrize(
    "params, nash, joint",
    [
        (BASE, 5.75, 10.5),
        (COST_15, 6.125, 10.75),
        (BASE.model_copy(update={"a": 12}), 6.75, 12.5),
    ],
)
def test_reference_prices_match_plan_section_14(params, nash, joint):
    ref = market().reference(params, AB)
    assert ref.nash == pytest.approx(nash)
    assert ref.joint_optimum == pytest.approx(joint)
    assert ref.nash_by_seller == pytest.approx({"A": nash, "B": nash})


def test_nash_with_one_firm_cost_shock_solves_both_first_order_conditions():
    params = BASE.model_copy(update={"c_offset": {"B": 0.5}})
    ref = market().reference(params, AB)
    assert ref.nash_by_seller["A"] == pytest.approx(5.84375)
    assert ref.nash_by_seller["B"] == pytest.approx(6.03125)


def test_three_sellers_with_mean_rival_price_keep_the_symmetric_formula():
    ref = market().reference(BASE, ("A", "B", "C"))
    assert ref.nash == pytest.approx(5.75)
    assert ref.joint_optimum == pytest.approx(10.5)


def test_best_response_is_the_nearest_grid_price_to_the_continuous_best_response():
    m = market()
    assert m.best_response("A", {"B": 10.5}, BASE) == pytest.approx(7.35)
    assert m.best_response("A", {"B": 5.75}, BASE) == pytest.approx(5.75)
    assert m.best_response("A", {"B": 10.5}, COST_15) == pytest.approx(7.6)


def test_nash_price_is_a_fixed_point_of_the_best_response():
    m = market()
    assert m.best_response("A", {"B": 5.75}, BASE) == pytest.approx(5.75)


def test_settle_computes_quantity_profit_and_consumer_surplus():
    s = market().settle({"A": 9.5, "B": 9.4}, COST_15)
    assert s.quantities == pytest.approx({"A": 5.15, "B": 5.4})
    assert s.profits == pytest.approx({"A": 41.2, "B": 42.66})
    assert s.clamped == []


def test_consumer_surplus_at_a_symmetric_price_equals_q_squared_over_b_minus_d():
    p = 5.75
    q = 10 - (1.5 - 1.0) * p
    s = market().settle({"A": p, "B": p}, BASE)
    assert s.consumer_surplus == pytest.approx(q * q / 0.5)


def test_consumer_surplus_falls_when_both_prices_rise():
    m = market()
    assert m.settle({"A": 10.5, "B": 10.5}, BASE).consumer_surplus < m.settle({"A": 5.75, "B": 5.75}, BASE).consumer_surplus


def test_negative_demand_is_clamped_to_zero_and_reported():
    s = market().settle({"A": 14.0, "B": 1.0}, BASE)
    assert s.quantities["A"] == 0
    assert s.profits["A"] == 0
    assert s.clamped == ["A"]


def test_off_grid_price_is_rejected():
    with pytest.raises(ValueError):
        market().settle({"A": 5.77, "B": 5.75}, BASE)


def test_grid_snaps_and_clamps():
    g = PriceGrid(min=0, max=15, step=0.05)
    assert g.snap(7.3333) == pytest.approx(7.35)
    assert g.snap(-3) == 0
    assert g.snap(99) == 15
