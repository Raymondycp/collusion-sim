"""Linear differentiated demand: q_i = a − b·p_i + d·mean(p_−i). Closed forms for Nash, joint optimum and best response."""

from __future__ import annotations

from typing import Mapping, Sequence

from pydantic import Field, model_validator

from simcore.demand.base import DEMAND, REFERENCES, DemandModelSpec, Solver
from simschema.base import DemandParams, Frozen

EPS = 1e-9


class LinearParams(DemandParams):
    a: float = Field(description="Demand intercept")
    b: float = Field(gt=0, description="Own-price slope")
    d: float = Field(ge=0, description="Cross-price slope (on the mean rival price)")


def solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    n = len(rhs)
    m = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < EPS:
            raise ValueError("singular system: the demand parameters have no unique solution")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [m[i][n] / m[i][i] for i in range(n)]


class LinearDemand:
    """With one seller the rival term is 0."""

    name = "linear"

    @staticmethod
    def _cross(n: int, p: LinearParams) -> float:
        return p.d / (n - 1) if n > 1 else 0.0

    def raw_quantities(self, prices: Mapping[str, float], p: LinearParams) -> dict[str, float]:
        n, total = len(prices), sum(prices.values())
        k = self._cross(n, p)
        return {s: p.a - p.b * pi + k * (total - pi) for s, pi in prices.items()}

    def consumer_surplus(self, quantities: Mapping[str, float], p: LinearParams) -> float:
        """½·qᵀB⁻¹q: the surplus of the quadratic utility that gives this demand system (needs b > d)."""
        sellers = list(quantities)
        n, k = len(sellers), self._cross(len(sellers), p)
        b_matrix = [[p.b if i == j else -k for j in range(n)] for i in range(n)]
        q = [quantities[s] for s in sellers]
        x = solve(b_matrix, q)
        return 0.5 * sum(qi * xi for qi, xi in zip(q, x))

    def nash(self, sellers: Sequence[str], p: LinearParams) -> dict[str, float]:
        n, k = len(sellers), self._cross(len(sellers), p)
        matrix = [[2 * p.b if i == j else -k for j in range(n)] for i in range(n)]
        rhs = [p.a + p.b * p.cost_of(s) for s in sellers]
        return dict(zip(sellers, solve(matrix, rhs)))

    def joint_optimum(self, sellers: Sequence[str], p: LinearParams) -> dict[str, float]:
        n, k = len(sellers), self._cross(len(sellers), p)
        costs = [p.cost_of(s) for s in sellers]
        matrix = [[2 * p.b if i == j else -2 * k for j in range(n)] for i in range(n)]
        rhs = [p.a + p.b * costs[i] - k * (sum(costs) - costs[i]) for i in range(n)]
        return dict(zip(sellers, solve(matrix, rhs)))

    def best_response(self, seller: str, rival_prices: Mapping[str, float], p: LinearParams) -> float:
        n = len(rival_prices) + 1
        k = self._cross(n, p)
        return (p.a + p.b * p.cost_of(seller) + k * sum(rival_prices.values())) / (2 * p.b)


def checks(p: LinearParams, sellers: Sequence[str]) -> list[str]:
    if not p.d < p.b:
        return [f"d ({p.d}) must be below b ({p.b})"]
    demand = LinearDemand()
    out = []
    for name, prices in (("Nash", demand.nash(sellers, p)), ("joint optimum", demand.joint_optimum(sellers, p))):
        if min(demand.raw_quantities(prices, p).values()) <= 0:
            out.append(f"quantity <= 0 at the {name}")
    return out


class LinearTargets(Frozen):
    """Symmetric targets. The solution does not depend on the number of sellers (the rival term uses the mean price)."""

    nash: float = Field(description="Target Nash price")
    joint_optimum: float = Field(description="Target joint-optimum price")
    b: float = Field(default=1.5, gt=0)
    c: float = Field(default=1.0, ge=0)

    @model_validator(mode="after")
    def _order(self) -> "LinearTargets":
        if not self.c < self.nash <= self.joint_optimum:
            raise ValueError("need c < nash <= joint_optimum")
        return self


def solve_targets(t: LinearTargets) -> LinearParams:
    """From the two first-order conditions: 2b·N = a + b·c + d·N and 2(b − d)·J = a + b·c − d·c."""
    d = 2 * t.b * (t.joint_optimum - t.nash) / (2 * t.joint_optimum - t.nash - t.c)
    a = (2 * t.b - d) * t.nash - t.b * t.c
    return LinearParams(a=round(a, 10), b=t.b, d=round(d, 10), c=t.c)


LINEAR = DEMAND.register(
    DemandModelSpec(
        id="linear",
        version="1",
        name="Linear differentiated demand",
        formula="q_i = a − b·p_i + d·mean(p_−i)",
        description="Closed forms for Nash, joint optimum and best response. Stable when d < b.",
        params_model=LinearParams,
        factory=LinearDemand,
        references=REFERENCES,
        checks=checks,
        solver=Solver(input_model=LinearTargets, solve=solve_targets),
        labels={
            "a": "Demand intercept",
            "b": "Own-price slope",
            "d": "Cross-price slope",
            "c": "Marginal cost",
            "c_offset": "Per-seller cost offset",
        },
    )
)
