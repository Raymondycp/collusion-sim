from __future__ import annotations

import random
from typing import Any, Literal

from pydantic import Field, model_validator

from simcore.demand import DEMAND
from simcore.generators import GENERATORS, LINEAR_BASE, Common, ScenarioGenerator, _draft
from simcore.scenario import SELLER_IDS, DemandRef, Event, Scenario
from simcore.shocks import Shock
from simschema.base import Frozen

# random: base params and shocks sampled inside bounds, for any registered demand model.


class ShockBound(Frozen):
    path: str = Field(pattern=r"^(/[^/]+)+$")
    op: Literal["add", "mul"] = "add"
    min: float
    max: float


class RandomInputs(Common):
    demand_model: str = "linear"
    params: dict[str, Any] = Field(
        default_factory=lambda: dict(LINEAR_BASE), description="Base params; paths in param_bounds are resampled"
    )
    param_bounds: dict[str, tuple[float, float]] = Field(default_factory=lambda: {"/a": (9.0, 11.0), "/c": (0.8, 1.2)})
    shock_bounds: list[ShockBound] = Field(
        default_factory=lambda: [ShockBound(path="/c", op="add", min=-0.6, max=0.6), ShockBound(path="/a", op="mul", min=0.85, max=1.15)]
    )
    n_events: int = Field(default=2, ge=0, le=4)
    placebo_share: float = Field(default=0.25, ge=0, le=1, description="Chance that an event is news only")
    attempts: int = Field(default=20, ge=1, le=100, description="Resample until the check passes, up to this many times")
    shock_news: Literal["random", "public", "private"] = Field(
        default="random",
        description="Who is told about each event (shock or news only): public = every seller, private = informed_seller "
        "only, random = a random subset per event. Set after the draw, so it changes nothing else in the scenario",
    )
    informed_seller: str | None = Field(default=None, description="With shock_news private: the one seller told about every event")

    @model_validator(mode="after")
    def _informed(self) -> "RandomInputs":
        if (self.shock_news == "private") != (self.informed_seller is not None):
            raise ValueError("informed_seller is set exactly when shock_news is private")
        if self.informed_seller is not None and self.informed_seller not in SELLER_IDS[: self.n_sellers]:
            raise ValueError(f"informed_seller {self.informed_seller} is not a seller")
        return self

    required_shock_paths: list[str] = Field(
        default_factory=list,
        description="Resample until every listed path has at least one real shock (for example /c: a common cost shock)",
    )


def _set(params: dict, path: str, value: float) -> dict:
    return Shock(path=path, op="set", value=value).apply(params)


def _news(label: str, up: bool, effect: int) -> str:
    return f"{label} will {'rise' if up else 'fall'} from round {effect} for a few rounds."


def _sample(inputs: RandomInputs, rng: random.Random) -> Scenario:
    spec = DEMAND.get(inputs.demand_model)
    params = dict(inputs.params)
    for path, (lo, hi) in sorted(inputs.param_bounds.items()):
        params = _set(params, path, round(rng.uniform(lo, hi), 2))
    sellers = list(SELLER_IDS[: inputs.n_sellers])
    events, starts = [], sorted(rng.sample(range(3, 14), inputs.n_events))
    for announce in starts:
        visible = sorted(rng.sample(sellers, rng.randint(1, len(sellers))))
        if not inputs.shock_bounds or rng.random() < inputs.placebo_share:
            events.append(
                Event(announce=announce, visible=visible, news="Analysts expect a normal season with no change in costs or demand.")
            )
            continue
        bound = rng.choice(inputs.shock_bounds)
        value = round(rng.uniform(bound.min, bound.max), 2)
        neutral = 1.0 if bound.op == "mul" else 0.0
        if value == neutral:
            value = bound.max if bound.max != neutral else bound.min
        effect = announce + rng.randint(0, 2)
        length = rng.randint(2, 17 - effect)
        label = spec.labels.get(bound.path.split("/")[1], bound.path)
        events.append(
            Event(
                announce=announce,
                effect=effect,
                length=length,
                shocks=[Shock(path=bound.path, op=bound.op, value=value)],
                visible=visible,
                news=_news(label, value > neutral, effect),
            )
        )
    if inputs.shock_news != "random":  # last, so public and private scenarios of one seed differ only in the audience
        audience = sellers if inputs.shock_news == "public" else [inputs.informed_seller]
        events = [e.model_copy(update={"visible": audience}) for e in events]
    return _draft(inputs, DemandRef(id=spec.id, version=spec.version, params=params), events)


def _run_random(inputs: RandomInputs, seed: int) -> Scenario:
    rng = random.Random(f"random:{seed}")
    draft = None
    for attempt in range(1, inputs.attempts + 1):
        try:
            draft = _sample(inputs, rng)
        except ValueError:
            continue
        shocked = {sh.path for e in draft.events for sh in e.shocks}
        if draft.check().ok and set(inputs.required_shock_paths) <= shocked:
            return draft.model_copy(update={"provenance": {"attempt": attempt}})
    if draft is None:
        raise ValueError("no sample fits the demand model; widen or fix the bounds")
    return draft.model_copy(update={"provenance": {"attempt": inputs.attempts, "check_failed": True}})


GENERATORS.register(
    ScenarioGenerator(
        id="random",
        version="2",  # 2: shock_news applies to every event, not only /c shocks
        name="Random within bounds",
        input_model=RandomInputs,
        run=_run_random,
        description=(
            "Samples base params and shocks inside the given bounds with the seed; resamples until the scenario check passes. "
            "Deterministic for a seed."
        ),
    )
)
