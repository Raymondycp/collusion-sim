"""One feature row per parent episode. Pure: the caller reads the store and passes events, children and scenario facts.

Each feature gets views built from its own declaration only: the declared event types, tier A only for layer A, never
the header. Raw values per episode; nothing here reads another row (CR-17 U4-5).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from features.base import REGISTRY, BranchView, EpisodeView, Feature, Missing, ScenarioFacts
from simschema.events import EventBase, Intervention


@dataclass(frozen=True)
class Child:
    kind: str
    run_id: str
    status: str
    fork_t: int
    intervention: Intervention
    events: tuple[EventBase, ...]


def view(
    cls: type[Feature],
    sellers: Sequence[str],
    events: Iterable[EventBase],
    facts: ScenarioFacts | None,
    trail: Sequence[dict] | None = None,
) -> EpisodeView:
    """A feature with access trail also gets the run's trail lines; no other feature does (DECISIONS-14 §3.1)."""
    allowed = frozenset(cls.inputs)
    kept = tuple(e for e in events if e.type in allowed and e.tier != "C" and (cls.data_layer == "B" or e.tier == "A"))
    lines = tuple(trail or ()) if cls.access == "trail" else None
    return EpisodeView(tuple(sellers), kept, allowed, facts if cls.scenario else None, lines)


def compute(
    cls: type[Feature],
    sellers: Sequence[str],
    events: Sequence[EventBase],
    children: Sequence[Child],
    facts: ScenarioFacts | None,
    evaluations: list[dict],
    trail: Sequence[dict] | None = None,
) -> tuple[float | None, str | None]:
    """(value, None) or (None, reason). A trail feature with no trail lines is missing with reason no_trail."""
    if cls.access == "trail" and not trail:
        return None, "no_trail"
    branches = [
        BranchView(c.kind, c.fork_t, c.status, view(cls, sellers, c.events, facts), probe=c.intervention if c.kind == "probe" else None)
        for c in children
        if c.kind in cls.branches
    ]
    try:
        return cls().compute(view(cls, sellers, events, facts, trail), branches, evaluations), None
    except Missing as m:
        return None, m.reason


def feature_columns(ids: Sequence[str]) -> list[str]:
    cols = [c for i in ids for c in (i, f"{i}_missing")]
    if "ablation_effect" in ids:
        at = cols.index("ablation_effect_missing") + 1
        cols[at:at] = NOOP_COLUMNS
    return cols


NOOP_COLUMNS = ("ablation_noop_rounds", "ablation_noop_share", "ablation_noop_share_all")
"""Manipulation check of the shuffle ablation, beside ablation_effect (lead, 22 Oct; CR-68). Not features: no rule reads
them as evidence. They are here so that features_code_hash covers how they are counted."""


def _reads(o) -> bool:
    """The observation reads a channel: it records a no-op, or it holds messages. An act call under c3_isolation (channels
    masked) and a seller with no read grant are not readers."""
    return "channels" not in o.masked and (o.mask_noop is True or bool(o.recent_messages))


def noop_columns(children: Sequence[Child]) -> dict:
    """Per completed shuffle ablation child, over its rounds from the fork (rounds before the first message count too):
    the rounds in which any reader's observation records mask_noop (the shuffle changed nothing that reader saw; the
    lead's rule), and the rounds in which every reader's did. rounds: the "any" rounds summed over the shuffle children.
    share and share_all: the largest child share, so share 1.0 means one child was a no-op in every round, a failed
    manipulation. Null without a completed shuffle child that recorded mask_noop (a null or clear ablation, a child that
    did not complete, or an episode written before contract 0.23.0, where every mask_noop is null; CR-68 2)."""
    counts = []
    for c in children:
        if c.kind != "ablation" or c.status != "completed" or getattr(c.intervention, "mode", None) != "shuffle":
            continue
        seen = [e for e in c.events if e.type == "observed" and e.t >= c.fork_t]
        if all(e.observation.mask_noop is None for e in seen):
            continue
        by_round: dict[int, list] = {}
        for e in seen:
            by_round.setdefault(int(e.t), []).append(e.observation)
        readers = {t: [o for o in obs if _reads(o)] for t, obs in by_round.items()}
        any_ = sum(any(o.mask_noop for o in obs) for obs in by_round.values())
        all_ = sum(bool(r) and all(o.mask_noop for o in r) for r in readers.values())
        counts.append((any_, all_, len(by_round)))
    if not counts:
        return dict.fromkeys(NOOP_COLUMNS)
    return {
        "ablation_noop_rounds": sum(n for n, _, _ in counts),
        "ablation_noop_share": max(n / r for n, _, r in counts),
        "ablation_noop_share_all": max(a / r for _, a, r in counts),
    }


def row_values(
    ids: Sequence[str],
    sellers: Sequence[str],
    events: Sequence[EventBase],
    children: Sequence[Child],
    facts: ScenarioFacts | None,
    evaluations: list[dict],
    parent_status: str,
    trail: Sequence[dict] | None = None,
) -> dict:
    """The feature columns of one row. A parent that did not complete gives null for every feature, with its status."""
    out: dict = {}
    for i in ids:
        value, reason = (
            compute(REGISTRY[i], sellers, events, children, facts, evaluations, trail)
            if parent_status == "completed"
            else (None, f"parent_{parent_status}")
        )
        out[i], out[f"{i}_missing"] = value, reason
    if "ablation_effect" in ids:
        out |= noop_columns(children) if parent_status == "completed" else dict.fromkeys(NOOP_COLUMNS)
    return out


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return format(v, ".12g")
    return str(v)


def to_csv(columns: Sequence[str], rows: Sequence[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return buf.getvalue()
