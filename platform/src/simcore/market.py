"""The market: price grid, settlement and reference prices over any registered demand model (simcore/demand/)."""

from __future__ import annotations

from typing import Mapping, Sequence

from pydantic import Field

from simcore.demand import DemandModel
from simschema.base import DemandParams, Frozen
from simschema.events import Reference

EPS = 1e-9


class PriceGrid(Frozen):
    min: float = 0.0
    max: float = 15.0
    step: float = Field(default=0.05, gt=0)

    def snap(self, price: float) -> float:
        k = round((price - self.min) / self.step)
        return round(min(max(self.min + k * self.step, self.min), self.max), 10)

    def contains(self, price: float) -> bool:
        return self.min - EPS <= price <= self.max + EPS and abs(self.snap(price) - price) < EPS


class Settlement(Frozen):
    prices: dict[str, float]
    quantities: dict[str, float]
    profits: dict[str, float]
    consumer_surplus: float
    clamped: list[str]


def _mean(values: Mapping[str, float]) -> float:
    return sum(values.values()) / len(values)


class Market:
    def __init__(self, demand: DemandModel, grid: PriceGrid) -> None:
        self.demand = demand
        self.grid = grid

    def settle(self, prices: Mapping[str, float], p: DemandParams) -> Settlement:
        off_grid = [s for s, v in prices.items() if not self.grid.contains(v)]
        if off_grid:
            raise ValueError(f"prices not on the grid: {off_grid}")
        raw = self.demand.raw_quantities(prices, p)
        q = {s: max(v, 0.0) for s, v in raw.items()}
        return Settlement(
            prices=dict(prices),
            quantities=q,
            profits={s: (prices[s] - p.cost_of(s)) * q[s] for s in prices},
            consumer_surplus=self.demand.consumer_surplus(q, p),
            clamped=sorted(s for s, v in raw.items() if v < 0),
        )

    def best_response(self, seller: str, rival_prices: Mapping[str, float], p: DemandParams) -> float:
        return self.grid.snap(self.demand.best_response(seller, rival_prices, p))

    def reference(self, p: DemandParams, sellers: Sequence[str]) -> Reference:
        nash = self.demand.nash(sellers, p)
        joint = self.demand.joint_optimum(sellers, p)

        def profit(prices: dict[str, float]) -> float:
            q = self.demand.raw_quantities(prices, p)
            return _mean({s: (prices[s] - p.cost_of(s)) * q[s] for s in prices})

        return Reference(
            nash=_mean(nash),
            joint_optimum=_mean(joint),
            nash_by_seller=nash,
            joint_by_seller=joint,
            nash_profit=profit(nash),
            joint_profit=profit(joint),
        )
