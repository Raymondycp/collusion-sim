"""Branch engine: fork a recorded run at round t from the snapshot of round t − 1, apply one intervention, run on.

A child is a normal run with its own run id and `branch` in its header. It records only its own rounds (from fork_t).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import Field

from simcore.agents import AgentPolicy
from simcore.recorder import Recorder
from simcore.run import HostPolicy, Run, RunSpec
from simcore.scenario import Scenario
from simcore.state import RunState
from simschema.base import Frozen
from simschema.events import AblationIntervention, BranchInfo, Intervention, PlaceboIntervention, ProbeIntervention

NULL_ABLATION_SAMPLE = 1000
"""First sample index of a null ablation child: fresh draws that no placebo child (1..n, n <= 10) shares."""


class BranchError(ValueError):
    """`snapshot`: the fork needs a parent snapshot that is not there (yet). `path`: the request field at fault."""

    def __init__(self, message: str, *, path: str | None = None, snapshot: bool = False) -> None:
        super().__init__(message)
        self.path, self.snapshot = path, snapshot


class BranchRequest(Frozen):
    at: int = Field(ge=1, description="fork_t: the first round that runs with the intervention")
    rounds: int = Field(ge=1, description="Rounds to run after the fork.")
    n: int = Field(default=1, ge=1, le=10, description="samples; placebo uses 3")
    intervention: Intervention


@dataclass(frozen=True)
class Child:
    spec: RunSpec
    branch: BranchInfo
    state: RunState
    rounds: range

    def run(self, scenario: Scenario, policies: dict[str, AgentPolicy], recorder: Recorder, host: HostPolicy | None = None) -> Run:
        return Run(self.spec, scenario, policies, recorder, host=host, state=self.state, branch=self.branch, rounds=self.rounds)


def plan_branches(
    parent: RunSpec,
    scenario: Scenario,
    snapshots: Mapping[int, tuple[str, str]],
    request: BranchRequest,
    child_ids: list[str],
    created_at: str,
) -> list[Child]:
    """`snapshots` maps round t -> (snapshot_hash, blob) of the parent, as the recorder sinks store them."""
    fork = request.at
    if not snapshots or fork > max(snapshots):
        raise BranchError(f"the parent did not run round {fork}", path="at", snapshot=True)
    if len(child_ids) != request.n or len(set(child_ids)) != request.n:
        raise BranchError(f"need {request.n} distinct child run ids, got {child_ids}")
    _check(parent, request)
    blob = _snapshot(snapshots, fork - 1) if fork > 1 else None
    # Probe and placebo children keep the parent's labels. An ablation changes the condition, so its children carry no label.
    # An ablation of an ablation child keeps the original labels as provenance.
    origin = parent.labels or parent.parent_labels
    # It also leaves the condition, so a group-by on condition_id never counts it in the parent's group (CR-2b m-b1).
    labels = (
        {"labels": None, "parent_labels": origin, "condition_id": None, "condition_hash": None}
        if isinstance(request.intervention, AblationIntervention)
        else {}
    )
    rounds = range(fork, fork + request.rounds)
    # Placebo children are fresh null draws, numbered from 1: sample 0 would replay the parent's own calls from the cache.
    # Probe and ablation children start at 0, so they replay the parent until the first changed message. A null ablation
    # changes no message, so from 0 it would copy the parent: it draws fresh samples, apart from the placebo numbers.
    iv = request.intervention
    first = (
        1
        if isinstance(iv, PlaceboIntervention)
        else NULL_ABLATION_SAMPLE
        if isinstance(iv, AblationIntervention) and iv.ablated == "none"
        else 0
    )
    return [
        Child(
            spec=parent.model_copy(update={"run_id": run_id, "created_at": created_at, "cloned_from": None, **labels}),
            branch=BranchInfo(parent_run_id=parent.run_id, fork_t=fork, intervention=request.intervention, sample_index=first + i),
            state=RunState.restore(blob) if blob else RunState(),
            rounds=rounds,
        )
        for i, run_id in enumerate(child_ids)
    ]


def _check(parent: RunSpec, request: BranchRequest) -> None:
    iv = request.intervention
    if isinstance(iv, ProbeIntervention):
        if iv.target not in {s.id for s in parent.sellers()}:
            raise BranchError(f"probe target {iv.target} is not a seller of the run", path="intervention/target")
        if iv.rounds > request.rounds:
            raise BranchError("the probe lasts longer than the branch", path="intervention/rounds")
    if isinstance(iv, AblationIntervention) and iv.ablated == "channel" and iv.channel_id not in {c.id for c in parent.channels()}:
        raise BranchError(f"channel {iv.channel_id} is not a channel of the run", path="intervention/channel_id")


def _snapshot(snapshots: Mapping[int, tuple[str, str]], t: int) -> str:
    if t not in snapshots:
        raise BranchError(f"no snapshot for round {t}", path="at", snapshot=True)
    expected, blob = snapshots[t]
    if "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest() != expected:
        raise BranchError(f"snapshot of round {t} does not match its hash", path="at", snapshot=True)
    return blob
