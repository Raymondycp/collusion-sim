"""Recorded series of one run, one value per settled round. Built from SimEvents only (never from live state)."""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from simschema.events import EventBase

Value = float | None

# Series prefix -> data layer. A = logs only. B = reference values from the payoff model. C (plan text) is never a series.
LAYER = {
    "price": "A",
    "quantity": "A",
    "profit": "A",
    "posts": "A",
    "queries": "A",
    "nash": "B",
    "joint_optimum": "B",
    "best_response": "B",
}


class LayerError(ValueError):
    pass


def layer_of(name: str) -> str:
    prefix = name.split(".")[0]
    if prefix not in LAYER:
        raise LayerError(f"{name}: not a recorded series (layer C and unknown series are never inputs)")
    return LAYER[prefix]


@dataclass(frozen=True)
class SeriesView:
    t: tuple[int, ...]
    sellers: tuple[str, ...]
    data: Mapping[str, tuple[Value, ...]]
    allowed: tuple[str, ...] | None = field(default=None)

    def __getitem__(self, name: str) -> list[Value]:
        if self.allowed is not None and not any(fnmatch.fnmatchcase(name, p) for p in self.allowed):
            raise LayerError(f"{name} is not a declared input")
        return list(self.data.get(name, (None,) * len(self.t)))

    def upto(self, t: int) -> "SeriesView":
        n = sum(1 for x in self.t if x <= t)
        return SeriesView(self.t[:n], self.sellers, {k: v[:n] for k, v in self.data.items()}, self.allowed)

    def restrict(self, patterns: Sequence[str]) -> "SeriesView":
        return SeriesView(self.t, self.sellers, self.data, tuple(patterns))

    @classmethod
    def from_events(cls, events: Iterable[EventBase]) -> "SeriesView":
        settled, refs, posts, queries = [], {}, Counter(), Counter()
        for e in events:
            if e.type == "settled":
                settled.append(e)
            elif e.type == "reference":
                refs[int(e.t)] = e
            elif e.type == "posted":
                posts[(int(e.t), e.agent_id)] += 1
            elif e.type == "tool_called":
                queries[(int(e.t), e.agent_id)] += 1
        sellers = tuple(sorted(settled[0].prices)) if settled else ()
        ts = tuple(int(e.t) for e in settled)
        data: dict[str, tuple[Value, ...]] = {}
        for s in sellers:
            data[f"price.{s}"] = tuple(e.prices[s] for e in settled)
            data[f"quantity.{s}"] = tuple(e.quantities[s] for e in settled)
            data[f"profit.{s}"] = tuple(e.profits[s] for e in settled)
            data[f"posts.{s}"] = tuple(float(posts[(t, s)]) for t in ts)
            data[f"queries.{s}"] = tuple(float(queries[(t, s)]) for t in ts)
            data[f"nash.{s}"] = tuple(refs[t].reference.nash_by_seller[s] if t in refs else None for t in ts)
            data[f"joint_optimum.{s}"] = tuple(refs[t].reference.joint_by_seller[s] if t in refs else None for t in ts)
            data[f"best_response.{s}"] = tuple(refs[t].best_response.get(s) if t in refs else None for t in ts)
        return cls(ts, sellers, data)
