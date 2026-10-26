"""The plan of one judge job: one call per (episode, view, transform, backend, paraphrase), in one random order seeded by
the job, interleaved across backends (DECISIONS-9 §18 "call order and timing"). Pure."""

from __future__ import annotations

import hashlib
import random

from judge.blocks import BLOCKS, CAP_PER_BACKEND, RETRY_RESERVE

__all__ = ["CAP_PER_BACKEND", "LEDGER_BACKENDS", "RETRY_RESERVE", "default_order_seed", "over_cap", "plan", "room"]
from judge.store import LedgerState, PlannedCall

LEDGER_BACKENDS = ("llm", "jev")
"""Backends counted in the judge ledger. The scripted judge makes no request and costs nothing."""


def default_order_seed(evaluation_id: str) -> int:
    return int.from_bytes(hashlib.sha256(f"order:{evaluation_id}".encode()).digest()[:4], "big")


def plan(episodes: list[tuple[str, str]], block_id: str, view: str, backends: list[str], order_seed: int) -> list[PlannedCall]:
    """episodes: (run_id, split) in a fixed order. The same inputs give the same plan."""
    block = BLOCKS[block_id]
    calls = [(run_id, split, b, k) for run_id, split in sorted(episodes) for b in sorted(backends) for k in block.paraphrases]
    random.Random(order_seed).shuffle(calls)
    return [PlannedCall(i, r, s, view, block.transform, b, k, block_id) for i, (r, s, b, k) in enumerate(calls)]


def room(state: LedgerState, new: list[PlannedCall]) -> list[dict]:
    """Ledger rows per ledger backend: used (charged first calls), retries (charged retries), held (planned calls of
    holding jobs), this job's calls, the block cap and the backend cap. First calls fill the blocks and the backend cap
    less the reserve; retries draw on the reserve only (CR-33 m3). used and held count the plan's blocks only; a job of a
    block outside the plan is checked against its own per-job cap (in_plan false)."""
    out = []
    for b in LEDGER_BACKENDS:
        mine = [p for p in new if p.backend == b]
        if not mine and not any(k[0] == b for k in state.first) and not state.retries.get(b):
            continue
        block = mine[0].block if mine else None
        row = {
            "backend": b,
            "used": sum(n for (bb, k), n in state.first.items() if bb == b and BLOCKS[k].in_plan),
            "retries_used": state.retries.get(b, 0),
            "held": sum(1 for p in state.held if p.backend == b and BLOCKS[p.block].in_plan),
            "calls": len(mine),
            "cap": CAP_PER_BACKEND,
            "reserve": RETRY_RESERVE,
        }
        if block is not None:
            row |= {
                "block": block,
                "block_used": state.first.get((b, block), 0),
                "block_held": sum(1 for p in state.held if p.backend == b and p.block == block),
                "block_cap": BLOCKS[block].cap_per_backend,
                "in_plan": BLOCKS[block].in_plan,
            }
        out.append(row)
    return out


def over_cap(rows: list[dict]) -> str | None:
    """The first cap this job would pass, as a sentence, or None."""
    for r in rows:
        if "block" in r and not r["in_plan"]:
            if r["calls"] > r["block_cap"]:
                return f"{r['backend']} block {r['block']}: {r['calls']} calls would pass its cap of {r['block_cap']} per job"
            continue
        if "block" in r and r["block_used"] + r["block_held"] + r["calls"] > r["block_cap"]:
            return (
                f"{r['backend']} block {r['block']}: {r['block_used']} used, {r['block_held']} held and {r['calls']} more "
                f"would pass its cap of {r['block_cap']}"
            )
        if r["used"] + r["held"] + r["calls"] > r["cap"] - r["reserve"]:
            limit = r["cap"] - r["reserve"]
            return (
                f"{r['backend']}: {r['used']} used, {r['held']} held and {r['calls']} more would pass {limit} (cap less the retry reserve)"
            )
    return None
