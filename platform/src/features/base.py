"""Episode features (DECISIONS-10 §5): one value per parent episode, the input of P2's decision rules.

A feature never sees the episode header, its labels or its condition (CR-17 U4-1). It gets an EpisodeView: the
seller ids and the events of the types it declares, and nothing else. The registry checks the declared types against
the event tiers (CR-17 U4-2): a layer-A feature reads tier-A events only, and no feature reads tier C.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from simschema.events import TIER, EventBase, ProbeIntervention

EvidenceType = Literal["conversation", "coalition_advantage", "dependence", "quality"]
"""quality: a check of the run (manipulation check, report), never evidence in a candidate rule (DECISIONS-22)."""
DataLayer = Literal["A", "B"]
Access = Literal["log", "model", "intervention", "trail"]
BranchKind = Literal["probe", "placebo", "ablation"]

SRC = Path(__file__).resolve().parents[1]

NEVER = ("run_started", "talk_text")
"""run_started: the header holds the labels, the condition and fields that give the condition away (c3_isolation,
prompt_hash). talk_text: model-written reply text that was never posted (tier B by DECISIONS-12; CR-46 n1)."""


class LayerError(ValueError):
    """A feature declares or reads an input that its layer or access does not allow."""


class Missing(Exception):
    """Raised by compute when the value cannot be computed. The row gets null and this reason, never 0 (CR-17 U4-7)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ScenarioFacts:
    """Facts from the stored scenario (hash checked by the caller). Model access: a log does not show them."""

    quiet_window: tuple[int, int]


@dataclass(frozen=True)
class EpisodeView:
    sellers: tuple[str, ...]
    events: tuple[EventBase, ...]
    allowed: frozenset[str]
    scenario_facts: ScenarioFacts | None = None
    trail_lines: tuple[dict, ...] | None = None

    @property
    def trail(self) -> tuple[dict, ...]:
        """The run's trail.jsonl lines (DECISIONS-14 §3.1): only for a feature with access trail."""
        if self.trail_lines is None:
            raise LayerError("the trail is not an input of this feature (access trail)")
        return self.trail_lines

    def of(self, event_type: str) -> list[Any]:
        if event_type not in self.allowed:
            raise LayerError(f"{event_type} is not a declared input")
        return [e for e in self.events if e.type == event_type]

    @property
    def scenario(self) -> ScenarioFacts:
        if self.scenario_facts is None:
            raise LayerError("the scenario is not a declared input")
        return self.scenario_facts


@dataclass(frozen=True)
class BranchView:
    """One branch child: its kind, fork round, end status and events, and for a probe its target, price and rounds.
    The ablation's intervention is not shown: `ablated` and `channel_id` tell whether the group has a private channel."""

    kind: BranchKind
    fork_t: int
    status: str
    episode: EpisodeView
    probe: ProbeIntervention | None = None


def source_modules(roots: Iterable[str], src: Path = SRC) -> dict[str, Path]:
    """The repo modules in roots, their parent packages, and every repo module they import, by name. Static imports
    only; a module outside src (stdlib, packages) is left out."""
    found: dict[str, Path] = {}
    todo = list(roots)
    while todo:
        name = todo.pop()
        base = src.joinpath(*name.split("."))
        path = next((f for f in (base.with_suffix(".py"), base / "__init__.py") if f.is_file()), None)
        if name in found or path is None:
            continue
        found[name] = path
        todo += [name.rsplit(".", 1)[0]] if "." in name else []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                todo += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                todo += [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
    return found


def source_hash(roots: Iterable[str], src: Path = SRC) -> str:
    """sha256 over the names and sources of source_modules(roots), in name order: an edit in any helper changes it."""
    h = hashlib.sha256()
    for name, path in sorted(source_modules(roots, src).items()):
        h.update(f"{name}\n{path.read_text(encoding='utf-8')}\n".encode())
    return h.hexdigest()


def files_hash(names: Iterable[str], src: Path = SRC) -> str:
    """sha256 over the names and sources of exactly these modules, with no import followed."""
    h = hashlib.sha256()
    for name in sorted(names):
        h.update(f"{name}\n{src.joinpath(*name.split('.')).with_suffix('.py').read_text(encoding='utf-8')}\n".encode())
    return h.hexdigest()


JUDGE_CODE = (
    "judge.aggregate",
    "judge.blocks",
    "judge.items",
    "judge.llm_judge",
    "judge.scoring",
    "judge.transcript",
    "judge.verify",
    "judge.views",
)
"""The files that shape the judge scores (item 3 J4): blocks (paraphrases and transform per block), item set and prompts,
request, parser, result status and retry rule, quote check, transcript views and their echo mask, aggregation and the join
into the feature table. Their files, not their imports, are in features_code_hash, so they cannot change between dev and
test unseen. The job runner (services.evaluations), the table builder (services.features) and the judge listings
(services.judging) are not, so a runner fix, a listing fix or a seller-code edit does not lock the research (lead, 22 Oct)."""


class Feature:
    """Subclass, set the class fields, implement compute. Register with @REGISTRY.register."""

    id: ClassVar[str]
    version: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    evidence_type: ClassVar[EvidenceType]
    data_layer: ClassVar[DataLayer]
    access: ClassVar[Access]
    inputs: ClassVar[tuple[str, ...]]
    """Event types read from the parent and from each child."""
    scenario: ClassVar[bool] = False
    branches: ClassVar[tuple[BranchKind, ...]] = ()

    def compute(self, parent: EpisodeView, branches: list[BranchView], evaluations: list[dict]) -> float:
        raise NotImplementedError

    @classmethod
    def code_hash(cls) -> str:
        """source_hash of the feature's module and the view builder (CR-23 M1): an edit there, or in any helper they
        import, changes the hash without a version bump."""
        return source_hash([cls.__module__, "features.table"])

    @classmethod
    def spec(cls) -> dict:
        return {
            "id": cls.id,
            "version": cls.version,
            "name": cls.name,
            "description": cls.description,
            "evidence_type": cls.evidence_type,
            "data_layer": cls.data_layer,
            "access": cls.access,
            "inputs": list(cls.inputs),
            "scenario": cls.scenario,
            "branches": list(cls.branches),
            "code_hash": cls.code_hash(),
        }


def check_inputs(cls: type[Feature]) -> None:
    for t in cls.inputs:
        if t in NEVER or t not in TIER:
            raise LayerError(f"{cls.id}: {t} is never a feature input")
        if TIER[t] == "C":
            raise LayerError(f"{cls.id}: {t} is tier C (model-written), never a feature input")
    if cls.data_layer == "A" and cls.access != "log":
        raise LayerError(f"{cls.id}: a layer-A feature has access log")
    if cls.access == "log" and (cls.scenario or cls.branches or any(TIER[t] != "A" for t in cls.inputs)):
        raise LayerError(f"{cls.id}: access log reads tier-A events of the parent only")
    if cls.access == "model" and cls.branches:
        raise LayerError(f"{cls.id}: a feature that reads branch children has access intervention")
    if cls.access == "intervention" and not cls.branches:
        raise LayerError(f"{cls.id}: access intervention needs a branch kind")
    if cls.access == "trail" and (cls.data_layer != "B" or cls.branches):
        raise LayerError(f"{cls.id}: access trail is layer B and reads the parent's trail only (DECISIONS-14 §3.1)")


class FeatureRegistry:
    def __init__(self) -> None:
        self._by_id: dict[str, type[Feature]] = {}

    def register(self, cls: type[Feature]) -> type[Feature]:
        check_inputs(cls)
        if cls.id in self._by_id:
            raise ValueError(f"feature {cls.id} is registered twice")
        self._by_id[cls.id] = cls
        return cls

    def __getitem__(self, feature_id: str) -> type[Feature]:
        return self._by_id[feature_id]

    def ids(self, layer: DataLayer | None = None) -> list[str]:
        """Registry order. layer A: the log-only table; layer B (or None): every feature."""
        return [i for i, c in self._by_id.items() if layer != "A" or c.data_layer == "A"]

    def code_hash(self) -> str:
        """features_code_hash: source_hash over every registered feature's module and the view builder, with files_hash of
        the judge columns' join and aggregation, so the freeze covers all of them."""
        features = source_hash([*sorted({c.__module__ for c in self._by_id.values()}), "features.table"])
        return hashlib.sha256(f"{features}\n{files_hash(JUDGE_CODE)}".encode()).hexdigest()

    def specs(self, ids: Iterable[str] | None = None) -> list[dict]:
        return [self._by_id[i].spec() for i in (self.ids() if ids is None else ids)]


REGISTRY = FeatureRegistry()
