"""Indicator plug-ins: pure functions over a run's recorded series, like TradingView studies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from indicators.series import LayerError, SeriesView, Value, layer_of
from simschema.base import NoParams


class OutputSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    kind: Literal["line", "histogram", "marker", "band"]
    unit: str


class ParamsError(ValueError):
    def __init__(self, indicator_id: str, errors: list[dict]) -> None:
        super().__init__(f"{indicator_id}: invalid params")
        self.errors = errors


class Indicator:
    """Subclass, set the class fields, implement compute. The value at round t may use rounds <= t only."""

    id: ClassVar[str]
    version: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    inputs: ClassVar[tuple[str, ...]]
    data_layer: ClassVar[Literal["A", "B"]]
    pane: ClassVar[Literal["overlay", "separate"]]
    outputs: ClassVar[tuple[OutputSpec, ...]]
    Params: ClassVar[type[BaseModel]] = NoParams

    def compute(self, s: SeriesView, p: Any) -> dict[str, list[Value]]:
        raise NotImplementedError

    @classmethod
    def params_schema(cls) -> dict:
        schema = cls.Params.model_json_schema()
        return {"type": "object", "additionalProperties": False, "properties": schema.get("properties", {})}

    @classmethod
    def spec(cls) -> dict:
        return {
            "id": cls.id,
            "version": cls.version,
            "name": cls.name,
            "description": cls.description,
            "params_schema": cls.params_schema(),
            "inputs": list(cls.inputs),
            "data_layer": cls.data_layer,
            "pane": cls.pane,
            "outputs": [o.model_dump() for o in cls.outputs],
        }


class IndicatorRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, type[Indicator]] = {}

    def register(self, cls: type[Indicator]) -> type[Indicator]:
        for name in cls.inputs:
            layer = layer_of(name)
            if cls.data_layer == "A" and layer != "A":
                raise LayerError(f"{cls.id} declares layer A but reads {name} (layer {layer})")
        if cls.id in self._by_id:
            raise ValueError(f"indicator {cls.id} is registered twice")
        self._by_id[cls.id] = cls
        return cls

    def __getitem__(self, ind_id: str) -> type[Indicator]:
        return self._by_id[ind_id]

    def __contains__(self, ind_id: str) -> bool:
        return ind_id in self._by_id

    def ids(self) -> list[str]:
        return sorted(self._by_id)

    def specs(self) -> list[dict]:
        return [self._by_id[i].spec() for i in self.ids()]


REGISTRY = IndicatorRegistry()


@dataclass(frozen=True)
class Instance:
    """One indicator computed with one set of params, unrounded. services.read.indicator_body makes the API body."""

    indicator_id: str
    version: str
    params: dict[str, Any]
    until: int
    t: tuple[int, ...]
    outputs: dict[str, list[Value]]


def instance(series: SeriesView, ind_id: str, params: dict, until: int | None = None) -> Instance:
    cls = REGISTRY[ind_id]
    try:
        p = cls.Params.model_validate(params)
    except ValidationError as e:
        raise ParamsError(ind_id, [{"path": "/".join(map(str, err["loc"])), "message": err["msg"]} for err in e.errors()]) from None
    view = series.upto(until) if until is not None else series
    raw = cls().compute(view.restrict(cls.inputs), p)
    return Instance(
        indicator_id=cls.id,
        version=cls.version,
        params=p.model_dump(),
        until=view.t[-1] if view.t else 0,
        t=tuple(view.t),
        outputs={k: list(vs) for k, vs in raw.items()},
    )
