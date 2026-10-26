"""Scenario generators (design time only; a run never calls one). Output is always a draft with provenance.

A generator is a registered spec: input model (its JSON Schema feeds the UI form) and generate(inputs, seed) -> draft Scenario.
Built-ins are modules in simcore/builtin/generators/, loaded in module-name order (bounded_random gives id `random`, then template),
so GET /scenario-generators lists `random` first. Both are deterministic for a given input and seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field, model_validator

from simcore.scenario import DemandRef, Event, Horizon, MarketSpec, Scenario
from simschema.base import Frozen
from simschema.plugins import load_all

LINEAR_BASE = {"a": 10.0, "b": 1.5, "d": 1.0, "c": 1.0}


@dataclass(frozen=True)
class ScenarioGenerator:
    id: str
    version: str
    name: str
    description: str
    input_model: type[BaseModel]
    run: Callable[[Any, int], Scenario]
    deterministic: bool = True
    paid: bool = False

    def generate(self, inputs: dict, seed: int = 0) -> Scenario:
        """Raises pydantic.ValidationError for bad inputs. The draft carries provenance; the caller runs the check."""
        parsed = self.input_model.model_validate(inputs)
        draft = self.run(parsed, seed)
        provenance = {"generator_id": self.id, "generator_version": self.version, "inputs": parsed.model_dump(mode="json"), "seed": seed}
        return draft.model_copy(update={"provenance": {**provenance, **(draft.provenance or {})}, "status": "draft", "hash": None})

    def info(self) -> dict:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "paid": self.paid,
            "deterministic": self.deterministic,
        }


class GeneratorRegistry:
    def __init__(self) -> None:
        self._gens: dict[str, ScenarioGenerator] = {}

    def register(self, gen: ScenarioGenerator) -> ScenarioGenerator:
        if gen.id in self._gens:
            raise ValueError(f"scenario generator {gen.id} is already registered")
        self._gens[gen.id] = gen
        return gen

    def get(self, gen_id: str) -> ScenarioGenerator:
        if gen_id not in self._gens:
            raise KeyError(f"unknown scenario generator {gen_id}")
        return self._gens[gen_id]

    def specs(self) -> list[ScenarioGenerator]:
        return list(self._gens.values())


GENERATORS = GeneratorRegistry()


class Common(Frozen):
    name: str = Field(default="Generated scenario", max_length=120)
    n_sellers: int = Field(default=2, ge=2, le=6)
    min_rounds: int = Field(default=30, ge=26)
    max_rounds: int = Field(default=40, ge=26, le=200)
    stop_prob: float = Field(default=0.1, ge=0, le=1)

    @model_validator(mode="after")
    def _rounds(self) -> "Common":
        if self.min_rounds > self.max_rounds:
            raise ValueError("min_rounds must not be above max_rounds")
        return self


def _draft(inputs: Common, demand: DemandRef, events: list[Event]) -> Scenario:
    return Scenario(
        id="scn_draft",
        name=inputs.name,
        market=MarketSpec(demand=demand),
        n_sellers=inputs.n_sellers,
        horizon=Horizon(min_rounds=inputs.min_rounds, max_rounds=inputs.max_rounds, stop_prob=inputs.stop_prob),
        events=events,
    )


load_all("simcore.builtin.generators")
