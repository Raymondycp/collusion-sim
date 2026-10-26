"""Demand-model registry. A model is one module in simcore/demand/ that registers a DemandModelSpec, plus known-answer tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol, Sequence

from pydantic import BaseModel, ValidationError

from simschema.base import DemandParams

REFERENCES = frozenset({"nash", "joint_optimum", "best_response"})


class DemandModel(Protocol):
    name: str

    def raw_quantities(self, prices: Mapping[str, float], p: DemandParams) -> dict[str, float]: ...

    def consumer_surplus(self, quantities: Mapping[str, float], p: DemandParams) -> float: ...

    def nash(self, sellers: Sequence[str], p: DemandParams) -> dict[str, float]: ...

    def joint_optimum(self, sellers: Sequence[str], p: DemandParams) -> dict[str, float]: ...

    def best_response(self, seller: str, rival_prices: Mapping[str, float], p: DemandParams) -> float: ...


@dataclass(frozen=True)
class Solver:
    """Deterministic helper: targets (for example Nash and joint-optimum prices) in, params out."""

    input_model: type[BaseModel]
    solve: Callable[[BaseModel], DemandParams]


@dataclass(frozen=True)
class DemandModelSpec:
    id: str
    version: str
    name: str
    formula: str
    params_model: type[DemandParams]
    factory: Callable[[], DemandModel]
    references: frozenset[str] = frozenset()
    checks: Callable[[DemandParams, Sequence[str]], list[str]] = lambda p, sellers: []
    solver: Solver | None = None
    description: str = ""
    labels: Mapping[str, str] = field(default_factory=dict)

    @property
    def research_allowed(self) -> bool:
        return REFERENCES <= self.references

    def params_schema(self) -> dict:
        return self.params_model.model_json_schema()

    def parse(self, params: Mapping) -> DemandParams:
        """Raises ValueError with the pydantic message when the params do not fit the model."""
        try:
            return self.params_model.model_validate(dict(params))
        except ValidationError as e:
            raise ValueError(f"{self.id} params: {e.errors()[0]['loc']}: {e.errors()[0]['msg']}") from None

    def build(self) -> DemandModel:
        return self.factory()

    def info(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "formula": self.formula,
            "description": self.description,
            "params_schema": self.params_schema(),
            "labels": dict(self.labels),
            "references": sorted(self.references),
            "research_allowed": self.research_allowed,
            "solver_schema": self.solver.input_model.model_json_schema() if self.solver else None,
        }


class DemandRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], DemandModelSpec] = {}

    def register(self, spec: DemandModelSpec) -> DemandModelSpec:
        if (spec.id, spec.version) in self._specs:
            raise ValueError(f"demand model {spec.id}@{spec.version} is already registered")
        if not issubclass(spec.params_model, DemandParams):
            raise TypeError(f"{spec.id}: params_model must extend DemandParams")
        self._specs[(spec.id, spec.version)] = spec
        return spec

    def get(self, model_id: str, version: str | None = None) -> DemandModelSpec:
        """Latest version when version is None. Raises KeyError for an unknown id or version."""
        versions = sorted((v for i, v in self._specs if i == model_id), key=lambda v: [int(x) for x in v.split(".")])
        if not versions or (version is not None and version not in versions):
            raise KeyError(f"unknown demand model {model_id}@{version or 'latest'}")
        return self._specs[(model_id, version or versions[-1])]

    def __contains__(self, model_id: str) -> bool:
        return any(i == model_id for i, _ in self._specs)

    def specs(self) -> list[DemandModelSpec]:
        return [self._specs[k] for k in sorted(self._specs)]


DEMAND = DemandRegistry()
