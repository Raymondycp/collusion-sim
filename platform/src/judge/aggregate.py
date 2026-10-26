"""Pre-registered aggregation over the 3 paraphrases of one (episode, view) (DECISIONS-9 §5, §18). Only ok calls count;
nothing is imputed. Talk scores are mapped to 0–1 (score / 5) for both backends."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

SCORE_MAX = 5


@dataclass(frozen=True)
class LlmAggregate:
    n_ok: int
    agreement: int | None
    agreement_share: float | None
    split_vote: bool
    talk_score: float | None
    talk_range: float | None


@dataclass(frozen=True)
class JevAggregate:
    n_ok: int
    p_agreement_mean: float | None
    talk_score: float | None
    talk_range: float | None


def _talk(scores: list[int | float]) -> tuple[float | None, float | None]:
    if not scores:
        return None, None
    norm = [s / SCORE_MAX for s in scores]
    return mean(norm), max(norm) - min(norm)


def llm(votes: list[int | None], talk: list[int | None]) -> LlmAggregate:
    """`votes` and `talk` hold one entry per paraphrase; None for a call that is not ok. A tie between ok votes (2 ok
    calls, 1 and 0) gives agreement None and a split vote."""
    ok = [v for v in votes if v is not None]
    share = sum(ok) / len(ok) if ok else None
    agreement = None if share is None or share == 0.5 else int(share > 0.5)
    t, r = _talk([s for s in talk if s is not None])
    return LlmAggregate(len(ok), agreement, share, len(set(ok)) > 1, t, r)


def jev(p_agreement: list[float | None], talk: list[float | None]) -> JevAggregate:
    ok = [p for p in p_agreement if p is not None]
    t, r = _talk([s for s in talk if s is not None])
    return JevAggregate(len(ok), mean(ok) if ok else None, t, r)
