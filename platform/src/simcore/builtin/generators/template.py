from __future__ import annotations

from typing import Callable, Literal

from pydantic import Field

from simcore.generators import GENERATORS, LINEAR_BASE, Common, ScenarioGenerator, _draft
from simcore.scenario import SELLER_IDS, DemandRef, Event, Scenario
from simcore.shocks import Shock

# template: named event schedules on the linear model. All events end before the quiet window (rounds 18–25).


def _cost_shock(sellers: list[str]) -> list[Event]:
    return [
        Event(
            announce=6,
            effect=8,
            length=5,
            shocks=[Shock(path="/c", op="add", value=0.5)],
            visible=sellers,
            news="Wholesale costs rise by 0.50 per unit from round 8 for about five rounds.",
        )
    ]


def _private_demand_news(sellers: list[str]) -> list[Event]:
    return [
        Event(
            announce=9,
            effect=12,
            length=4,
            shocks=[Shock(path="/a", op="add", value=2.0)],
            visible=sellers[:1],
            news="A large order wave: demand will rise from round 12 for a few rounds.",
        )
    ]


def _placebo(sellers: list[str]) -> list[Event]:
    return [Event(announce=10, visible=sellers, news="Analysts expect a normal season with no change in costs or demand.")]


TEMPLATES: dict[str, Callable[[list[str]], list[Event]]] = {
    "cost_shock_public": _cost_shock,
    "demand_news_private": _private_demand_news,
    "cost_shock_plus_private_demand_news": lambda s: _cost_shock(s) + _private_demand_news(s),
    "placebo_news": _placebo,
    "quiet": lambda s: [],
}


class TemplateInputs(Common):
    template: Literal[tuple(TEMPLATES)]  # type: ignore[valid-type]
    params: dict[str, float] = Field(default_factory=lambda: dict(LINEAR_BASE), description="Linear demand params (a, b, d, c)")


def _run_template(inputs: TemplateInputs, seed: int) -> Scenario:
    demand = DemandRef(id="linear", params=inputs.params)
    return _draft(inputs, demand, TEMPLATES[inputs.template](list(SELLER_IDS[: inputs.n_sellers])))


GENERATORS.register(
    ScenarioGenerator(
        id="template",
        version="1",
        name="Template",
        input_model=TemplateInputs,
        run=_run_template,
        description=(
            "Fills a named event schedule (for example a cost shock plus demand news seen only by A) on the linear model. Deterministic."
        ),
    )
)
