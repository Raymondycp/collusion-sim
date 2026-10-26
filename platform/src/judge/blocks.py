"""Judge call blocks, the same for both backends (DECISIONS-9 §18, DECISIONS-13 §7). Each block caps the calls per backend;
the backend cap is the sum plus the retry reserve. A block with `conditions` judges only episodes of those conditions.
A block outside the plan (`in_plan` false: `imported`) is not in the backend cap or its sum: its cap is per job, it has no
retry, and its calls never use the plan's room (unit 5b, lead 22 Oct)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RETRY_RESERVE = 10
BRIEF_MAX_JUDGE_CALLS = 300
"""The brief's 100-300 judge calls per backend (DECISIONS-9 §18); a test holds CAP_PER_BACKEND to it (CR-64 finding 5)."""
IMPORTED_CAP_PER_JOB = 50
"""Calls per backend in one judge job of the imported block (lead, 22 Oct: its own per-job cap)."""
MAIN_CONDITIONS = ("C1", "C2a", "C3-accept")
"""The brief's three case types (DECISIONS-13 §7). C3-silent is a support contrast with its own one-wording block."""


@dataclass(frozen=True)
class Block:
    id: str
    name: str
    tag: Literal["required", "support"]
    splits: tuple[str, ...]
    views: tuple[str, ...]
    paraphrases: tuple[int, ...]
    cap_per_backend: int
    transform: str = "none"
    cache: bool = True
    conditions: tuple[str, ...] | None = None  # None: any condition
    in_plan: bool = True

    def body(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "tag": self.tag,
            "splits": list(self.splits),
            "views": list(self.views),
            "paraphrases": list(self.paraphrases),
            "cap_per_backend": self.cap_per_backend,
            "transform": self.transform,
            "cache": self.cache,
            "conditions": None if self.conditions is None else list(self.conditions),
            "in_plan": self.in_plan,
        }


BLOCKS: dict[str, Block] = {
    b.id: b
    for b in (
        Block(
            "wording", "Wording development (pilot seeds, disclosed)", "required", ("pilot",), ("talk_only", "talk_prices"), (1, 2, 3), 20
        ),
        Block(
            "main",
            "Main: every parent episode of the three case types, 3 paraphrases",
            "required",
            ("dev", "test"),
            ("talk_only",),
            (1, 2, 3),
            180,
            conditions=MAIN_CONDITIONS,
        ),
        Block(
            "support_one_wording",
            "C3-silent: every parent episode, paraphrase 1",
            "support",
            ("dev", "test"),
            ("talk_only",),
            (1,),
            20,
            conditions=("C3-silent",),
        ),
        Block("talk_prices", "Talk and prices view, dev half", "support", ("dev",), ("talk_prices",), (1,), 30, conditions=MAIN_CONDITIONS),
        Block("stability", "Stability repeat, 10 episodes", "support", ("dev",), ("talk_only",), (1,), 10, cache=False),
        Block("robustness", "Round shuffle, 10 episodes", "support", ("dev",), ("talk_only",), (1,), 10, transform="round_shuffle"),
        Block(
            "imported",
            "Imported logs (POST /imports), outside the plan, per job",
            "support",
            ("exploratory",),
            ("talk_only", "talk_prices"),
            (1,),
            IMPORTED_CAP_PER_JOB,
            in_plan=False,
        ),
    )
}
CAP_PER_BACKEND = sum(b.cap_per_backend for b in BLOCKS.values() if b.in_plan) + RETRY_RESERVE
