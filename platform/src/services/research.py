"""Research inputs (DECISIONS-10 §1–2, §4): condition files, the seed map, the freeze status, and the expansion of a
research config into one fully specified config per seed.

Labels come only from a condition file. A research config names a condition, a split and seeds; everything else that
defines the case group (scenario, channels, call mode, tool grants, prompts) comes from the files, so the stored
per-run config is the single source for resume and branching.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from features import LOADED_FEATURES_HASH
from features import REGISTRY as FEATURES
from judge import items as itemsets
from judge.items import PLACEHOLDER
from services import models_file
from services.catalog import Catalog
from services.configs import AgentConfig, ApiError, ChannelSettings, RunConfig, is_open
from simcore.callplan import CANNOT_SEE, CHANNEL_GRANTS, Setting, call_plan, open_channels
from simcore.roles import ROLES
from simcore.scenario import Scenario
from simcore.tools import TOOLS
from simschema.base import Frozen
from simschema.events import Capability, Labels, RunLimits, ToolGrant

CONDITION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}")
PROMPTS_FILE = "prompts.draft.yaml"
SEEDS_FILE = "seeds.yaml"
FROZEN_FILE = "frozen.json"
STAGES = ("conditions", "rule")
"""DECISIONS-11 §2.5: stage conditions (with the judge rubric) opens dev; stage rule (adds the decision rule) opens test."""
RESEARCH_FILES = {"decision_rule": "decision-rule.yaml", "judge_rubric": "judge-rubric.yaml", "models": "models.yaml"}
"""P2's decision rule, the judge rubric and the models of the research runs: a freeze needs all three (DECISIONS-11 §2,
CR-43 M1)."""
STAGE_OF = {"judge_rubric": "conditions", "models": "conditions", "decision_rule": "rule"}
"""The stage that freezes each research file. The judge scores dev episodes, so its rubric is fixed before dev (item 3 L1);
dev and test use the same models, so they are fixed before dev too (CR-43 M1)."""


def _stage_files(stage: str) -> list[str]:
    """The research files that a stage covers: its own, plus those of the stages before it."""
    upto = STAGES[: STAGES.index(stage) + 1]
    return [k for k in RESEARCH_FILES if STAGE_OF[k] in upto]


VARIANTS = ("v1", "v2")


def canonical_hash(content: dict) -> str:
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class SellerCondition(Frozen):
    pricing: str = Field(description="Path into prompts.draft.yaml (base, groups/<g>/pricing, ...)")
    talk: str | None = Field(default=None, description="Path of the talk prompt; null when the seller cannot post")
    grants: list[ToolGrant]


class ConditionFile(Frozen):
    id: str = Field(pattern=CONDITION_ID.pattern)
    version: int = Field(ge=1)
    status: Literal["draft", "frozen"]
    frozen_hash: str | None = Field(
        default=None, description="The condition hash when the lead froze it; set if and only if status is frozen"
    )
    group: Literal["C1", "C2a", "C2b", "C3", "C4", "LC"]
    sub_condition: Literal["responder_accepts", "responder_silent"] | None = None
    name: str
    tag: Literal["required", "support", "extension"]
    description: str
    labels: Labels
    market_preset: Literal["base", "elastic"]
    channels: ChannelSettings
    call_mode: Literal["one_call", "talk_and_act"]
    c3_isolation: bool
    transparency: Literal["rival_last_round", "opaque"]
    same_model_for_all_sellers: bool | None = None
    memory_summary: Literal["code", "llm", "none"] = Field(
        description="Required (CR-77 3), so no condition falls back to notes without saying so; research: none (D15 §1)"
    )
    observation_mode: Literal["push", "pull", "both"] = Field(description="Stated by every condition (DECISIONS-15 §3.2)")
    limits: RunLimits
    sellers: dict[str, SellerCondition] = Field(min_length=2)

    @model_validator(mode="after")
    def _rules(self) -> "ConditionFile":
        if (self.status == "frozen") != (self.frozen_hash is not None):
            raise ValueError("frozen_hash is set if and only if status is frozen")
        if (self.group == "C3") != (self.sub_condition is not None):
            raise ValueError("a sub_condition is set for C3 and only for C3")
        for sid, s in self.sellers.items():
            tools = {g.tool_id for g in s.grants}
            if ("get_rival_prices" in tools) != (self.transparency == "rival_last_round"):
                raise ValueError(f"seller {sid}: transparency {self.transparency} and the get_rival_prices grant disagree")
            if ("post" in tools) != (s.talk is not None):
                raise ValueError(f"seller {sid}: a talk prompt is set if and only if the seller has a post grant")
            if s.talk not in (None, s.pricing) and self.call_mode != "talk_and_act":
                raise ValueError(f"seller {sid}: a separate talk prompt needs call_mode talk_and_act")
            if tools & {"read_channel", "post"} and not (self.channels.public or self.channels.private_pairs):
                raise ValueError(f"seller {sid} has channel grants but the condition has no channel")
            # The file states the effective grants: no grant that a run switch would take away (CR-69 addendum 1)
            if "get_news" in tools and not self.channels.news:
                raise ValueError(f"seller {sid}: news is off, so the condition grants no get_news")
            for g in (g for g in s.grants if g.tool_id in CHANNEL_GRANTS):
                named = g.params.get("channels") or []
                if not named:
                    raise ValueError(f"seller {sid}: {g.tool_id} names no channel; leave the grant out")
                if closed := [c for c in named if not is_open(c, self.channels)]:
                    raise ValueError(f"seller {sid}: {g.tool_id} names {closed}, which the condition does not open")
        plan = self.plan()
        if refused := plan.refused:
            raise ValueError(refused[0])
        # A research file refuses what a run only reports: a seller without queries that cannot see a part (D13 §1)
        if blind := [n for p in plan.sellers.values() for n in p.notes if CANNOT_SEE in n]:
            raise ValueError(blind[0])
        return self

    def plan(self):
        """The condition's call plan (simcore.callplan): its refusals are the run's, so a file and a run refuse alike."""
        return call_plan(
            Setting(
                capabilities={sid: Capability(grants=s.grants) for sid, s in self.sellers.items()},
                channels=open_channels(list(self.sellers), self.channels.public, self.channels.private_pairs),
                call_mode=self.call_mode,
                c3_isolation=self.c3_isolation,
                observation_mode=self.observation_mode,
                limits=self.limits,
                memory_summary=self.memory_summary,
                news=self.channels.news,
                transparency=self.transparency,
            )
        )


class SellerPrompt(Frozen):
    system_prompt: str
    talk_prompt: str | None = None


class PromptVariant(Frozen):
    id: str
    sellers: dict[str, SellerPrompt]


class Condition(Frozen):
    """A condition file with its prompt paths resolved to texts and its grants normalised (default params filled, as a
    run header shows them). The hash covers everything except status."""

    file: ConditionFile
    capabilities: dict[str, Capability]
    prompt_variants: list[PromptVariant]

    @property
    def id(self) -> str:
        return self.file.id

    @property
    def hash(self) -> str:
        body = self.file.model_dump(mode="json", exclude={"status", "frozen_hash"}) | {
            "prompt_variants": [v.model_dump(mode="json") for v in self.prompt_variants]
        }
        return canonical_hash(body)

    def variant(self, seed: int) -> PromptVariant:
        """The prompt paraphrase of a seed: seed % 2 (never chosen by the client)."""
        return self.prompt_variants[seed % len(self.prompt_variants)]

    def body(self) -> dict:
        """The contract's Condition object."""
        f = self.file
        return {
            **f.model_dump(mode="json", exclude={"sellers"}),
            "hash": self.hash,
            "capabilities": {sid: c.model_dump(mode="json") for sid, c in self.capabilities.items()},
            "prompt_variants": [v.model_dump(mode="json") for v in self.prompt_variants],
        }


def _prompt(prompts: dict, path: str) -> dict[str, str]:
    node = prompts
    for part in path.split("/"):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"prompt path {path} is not in {PROMPTS_FILE}")
        node = node[part]
    if node == "base":
        node = prompts["base"]
    if not (isinstance(node, dict) and set(node) == set(VARIANTS)):
        raise ValueError(f"prompt path {path} does not give the variants {VARIANTS}")
    return node


def resolve(file: ConditionFile, prompts: dict) -> Condition:
    texts = {sid: (_prompt(prompts, s.pricing), _prompt(prompts, s.talk) if s.talk else None) for sid, s in file.sellers.items()}
    variants = [
        PromptVariant(
            id=v,
            sellers={
                sid: SellerPrompt(system_prompt=p[v].strip(), talk_prompt=t[v].strip() if t else None) for sid, (p, t) in texts.items()
            },
        )
        for v in VARIANTS
    ]
    seller = ROLES["seller"]
    caps = {sid: TOOLS.normalise(Capability(grants=s.grants), "seller", seller.tools) for sid, s in file.sellers.items()}
    return Condition(file=file, capabilities=caps, prompt_variants=variants)


class SeedEntry(Frozen):
    seed: int = Field(ge=0)
    split: Literal["pilot", "dev", "test"]
    shock_news: Literal["public", "private"] = Field(
        description="Designed seed factor: who is told about each shock (public: every seller; private: informed_seller only)"
    )
    informed_seller: str | None = Field(default=None, description="With private shock news: the one seller told")
    scenario_id: str
    scenario_hash: str


class GeneratorRef(Frozen):
    id: str
    version: str
    inputs: dict


class SeedMap(Frozen):
    """conditions/seeds.yaml: one frozen scenario per seed, the same for every condition (paired design)."""

    generator: GeneratorRef
    seeds: list[SeedEntry]

    @model_validator(mode="after")
    def _unique(self) -> "SeedMap":
        seeds = [e.seed for e in self.seeds]
        if len(set(seeds)) != len(seeds):
            raise ValueError("a seed is listed twice")
        return self

    @property
    def hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    def entry(self, seed: int) -> SeedEntry | None:
        return next((e for e in self.seeds if e.seed == seed), None)

    def split(self, name: str) -> list[int]:
        return [e.seed for e in self.seeds if e.split == name]


def quality_in_rule(text: str) -> list[dict]:
    """Detector places of the decision rule (features_allowed, candidate_rules[].features) that name a feature with
    evidence_type quality: never a detector (DECISIONS-23 entry 2). The manipulation check may name them."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    quality = {i for i in FEATURES.ids() if FEATURES[i].evidence_type == "quality"}
    places = [("features_allowed", doc.get("features_allowed"))]
    rules = doc.get("candidate_rules") if isinstance(doc.get("candidate_rules"), list) else []
    places += [(f"candidate_rules/{i}/features", r.get("features")) for i, r in enumerate(rules) if isinstance(r, dict)]
    return [
        {"path": f"{path}/{j}", "message": f"{name} is a run-quality column (evidence_type quality), not evidence"}
        for path, names in places
        if isinstance(names, list)
        for j, name in enumerate(names)
        if name in quality
    ]


@dataclass(frozen=True)
class ResearchBinding:
    """What a research run takes from the files besides its config: provenance and labels for the RunSpec."""

    condition_id: str
    condition_hash: str
    labels: Labels
    seeds_hash: str
    c3_isolation: bool
    config_hash: str


@dataclass(frozen=True)
class SeedRun:
    config: RunConfig
    scenario: Scenario
    binding: ResearchBinding


CHANNEL_TOOLS = ("read_channel", "post")
"""With the channel switches and c3_isolation, what a condition manipulates (affordance ablation). The rest of the tools
and the mode is the same in every condition (DECISIONS-15 §3.4)."""


def setting_problems(conditions: list[Condition]) -> list[dict]:
    """Each place where a condition differs from the first condition outside CHANNEL_TOOLS, the channel switches and
    c3_isolation: the observation mode, transparency, call mode, memory, news switch, limits, and each seller's query
    limit and every other grant (with its params) (CR-71 m1). The prompts are not compared: they are the condition's text."""

    def setting(c: Condition) -> dict:
        f = c.file
        out = {
            "observation_mode": f.observation_mode,
            "transparency": f.transparency,
            "call_mode": f.call_mode,
            "memory_summary": f.memory_summary,
            "channels/news": f.channels.news,
            "limits": f.limits.model_dump(mode="json"),
        }
        for sid, cap in sorted(c.capabilities.items()):
            out[f"sellers/{sid}/grants"] = {g.tool_id: g.params for g in cap.grants if g.tool_id not in CHANNEL_TOOLS}
            out[f"sellers/{sid}/max_queries_per_round"] = cap.max_queries_per_round
        return out

    if not conditions:
        return []
    first, *rest = sorted(conditions, key=lambda c: c.id)
    base = setting(first)
    problems = []
    for c in rest:
        mine = setting(c)
        for key in sorted(set(base) | set(mine)):
            if base.get(key) != mine.get(key):
                problems.append({"path": f"{c.id}/{key}", "message": f"{mine.get(key)} in {c.id}, {base.get(key)} in {first.id}"})
    return problems


CONDITION_FIELDS = (
    "scenario_id",
    "scenario_hash",
    "prompt_variant_id",
    "channels",
    "transparency",
    "call_mode",
    "observation_mode",
    "limits",
)


class Research:
    def __init__(self, conditions_dir: Path, catalog: Catalog, profile_of: Callable | None = None) -> None:
        """profile_of: provider profile id -> its record (base_url, protocol), for conditions/models.yaml."""
        self.dir, self.catalog, self.profile_of = Path(conditions_dir), catalog, profile_of

    def _prompts(self) -> dict:
        return yaml.safe_load((self.dir / PROMPTS_FILE).read_text(encoding="utf-8"))

    def _load(self, path: Path, prompts: dict) -> Condition:
        try:
            file = ConditionFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            if file.id != path.stem:
                raise ValueError(f"id {file.id} does not match the file name")
            return resolve(file, prompts)
        except (ValidationError, ValueError) as e:
            raise ApiError(500, "condition_invalid", f"Condition file {path.name} is invalid: {e}") from None

    def condition_files(self) -> list[Path]:
        return sorted(p for p in self.dir.glob("*.yaml") if p.name not in (PROMPTS_FILE, SEEDS_FILE, *RESEARCH_FILES.values()))

    def conditions(self) -> list[Condition]:
        prompts = self._prompts()
        return [self._load(p, prompts) for p in self.condition_files()]

    def condition(self, condition_id: str) -> Condition:
        path = self.dir / f"{condition_id}.yaml"
        if not CONDITION_ID.fullmatch(condition_id) or path not in self.condition_files():  # CR-29 m2: seeds, research files
            raise ApiError(404, "not_found", f"Condition {condition_id} not found.")
        return self._load(path, self._prompts())

    def seed_map(self) -> SeedMap:
        path = self.dir / SEEDS_FILE
        if not path.exists():
            raise ApiError(409, "seeds_missing", f"conditions/{SEEDS_FILE} does not exist. Run `python -m services.seeds make`.")
        try:
            return SeedMap.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except ValidationError as e:
            raise ApiError(500, "seeds_invalid", f"conditions/{SEEDS_FILE} is invalid: {e}") from None

    def seeds_body(self) -> dict:
        m = self.seed_map()
        return {
            "hash": m.hash,
            "pilot": m.split("pilot"),
            "dev": m.split("dev"),
            "test": m.split("test"),
            "test_locked": not self.freeze_status()["valid"],
            "seeds": [e.model_dump(mode="json") for e in m.seeds],
        }

    def current_hashes(self) -> dict[str, str]:
        """What a freeze covers, computed again from the files at each call (CR-17 U3-1): every condition, the seed map,
        every seed scenario, the feature code (features_code_hash), and the decision rule and judge rubric files if they
        exist (DECISIONS-10 §4, DECISIONS-11 §2)."""
        out = {f"condition:{c.id}": c.hash for c in self.conditions()}
        m = self.seed_map()
        out["seeds"] = m.hash
        for e in m.seeds:
            out[f"scenario:{e.scenario_id}"] = self.catalog.scenario(e.scenario_id).hash
        out["features"] = "sha256:" + FEATURES.code_hash()
        for key, name in RESEARCH_FILES.items():
            path = self.dir / name
            if path.exists():
                out[key] = _text_hash(path.read_text(encoding="utf-8"))
        return out

    def freeze_inputs(self) -> dict[str, str | None]:
        """Every freeze input with its hash now; null for a research file that is missing (DECISIONS-11 §1, fe's preview)."""
        try:
            now = self.current_hashes()
        except ApiError:
            return {}
        return {k: None for k in RESEARCH_FILES} | now

    # Text files edited from the web UI (DECISIONS-11 §1). Every save writes the file, so git shows the diff.

    def stored_file(self, name: str) -> dict:
        path = self.dir / name
        if not path.exists():
            return {"name": f"conditions/{name}", "text": None, "hash": None, "updated_at": None}
        text = path.read_text(encoding="utf-8")
        at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")
        return {"name": f"conditions/{name}", "text": text, "hash": _text_hash(text), "updated_at": at}

    def save_research_file(self, key: str, text: str, test_cells_used: int) -> dict:
        """decision-rule, judge-rubric or models: any YAML. The rubric and the models are refused once stage conditions is
        frozen (like a condition file). The rule may change until a test cell is used (CR-29 m1); a change after stage rule
        makes that stage invalid, so test locks again, and `invalidated_stage` says so. The models file gets base_url and
        protocol of each named profile from that profile (422 for an unknown profile); the file is then written as YAML
        again, so comments in it are not kept."""
        name = RESEARCH_FILES[key]
        stage = STAGE_OF[key]
        if test_cells_used or (stage == "conditions" and self.freeze_status()["stages"][stage]["frozen"]):
            why = f"{test_cells_used} test cells are used" if test_cells_used else f"stage {stage} is frozen"
            raise ApiError(409, "research_frozen", f"conditions/{name} cannot change: {why}.")
        data = _yaml(text)
        if key == "models":
            filled = models_file.fill(data, self._profile)
            text = text if filled == data else yaml.safe_dump(filled, sort_keys=False, allow_unicode=True)
        was = self.freeze_status()["stages"][stage]["valid"]
        _write(self.dir / name, _lf(text))
        now = self.freeze_status()["stages"][stage]["valid"]
        return self.stored_file(name) | {"invalidated_stage": stage if was and not now else None}

    def condition_file(self, condition_id: str) -> dict:
        self.condition(condition_id)  # 404 for an unknown id
        return self.stored_file(f"{condition_id}.yaml")

    def save_condition_file(self, condition_id: str, text: str) -> dict:
        """A draft condition only (409 condition_frozen). The new text must be a valid draft condition with the same id
        whose prompt paths resolve."""
        if self.condition(condition_id).file.status == "frozen":
            raise ApiError(409, "condition_frozen", f"Condition {condition_id} is frozen; it cannot change after the freeze.")
        try:
            file = ConditionFile.model_validate(_yaml(text))
            if file.id != condition_id:
                raise ValueError(f"id {file.id} does not match {condition_id}")
            if file.status != "draft":
                raise ValueError("status stays draft; POST /freeze freezes every condition")
            resolve(file, self._prompts())
        except ValidationError as e:
            errors = [{"path": "/".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()]
            raise ApiError(422, "validation_error", f"Condition {condition_id} is invalid.", errors) from None
        except ValueError as e:
            raise ApiError(422, "validation_error", f"Condition {condition_id} is invalid: {e}") from None
        _write(self.dir / f"{condition_id}.yaml", _lf(text))
        return self.condition_file(condition_id)

    def _stages(self) -> dict | None:
        """The stages stored in frozen.json, or None when there is no file. Raises ValueError for a file of another shape."""
        path = self.dir / FROZEN_FILE
        if not path.exists():
            return None
        stages = json.loads(path.read_text(encoding="utf-8"))["stages"]
        if not isinstance(stages, dict) or set(stages) - set(STAGES) or not stages:
            raise ValueError("unknown stages")
        for st in stages.values():
            st["hashes"] = dict(st["hashes"])
        return stages

    def _stage_problems(self, stage: str, stored: dict, now: dict[str, str]) -> list[str]:
        """What breaks one stage now: a stored hash that changed or is gone, an input it does not cover, a missing research
        file of the stage, and a condition that is not frozen with its own hash."""
        own = _stage_files(stage)
        now = {k: v for k, v in now.items() if k not in RESEARCH_FILES or k in own}
        problems = [f"{k} changed since the freeze" for k in sorted(stored) if k in now and now[k] != stored[k]]
        problems += [f"{k} is gone since the freeze" for k in sorted(set(stored) - set(now))]
        problems += [f"{k} is not covered by the freeze" for k in sorted(set(now) - set(stored))]
        problems += [f"conditions/{RESEARCH_FILES[k]} is missing" for k in own if k not in now and k not in stored]
        problems += [f"condition:{c.id} is not frozen with its hash" for c in self.conditions() if c.file.frozen_hash != c.hash]
        return problems

    def freeze_status(self) -> dict:
        """Two stages (DECISIONS-11 §2.5): `conditions` opens dev; `rule` opens test. The top level is the test gate
        (stage rule); `stages` gives both."""

        def stage(problems: list[str], st: dict | None) -> dict:
            st = st or {}
            return {
                "frozen": bool(st),
                "frozen_at": st.get("frozen_at"),
                "approved_by": st.get("approved_by"),
                "valid": not problems,
                "problems": problems,
            }

        try:
            stages = self._stages()
        except (ValueError, KeyError, TypeError, AttributeError):
            stages, broken = {}, [f"{FROZEN_FILE} is not valid"]
        else:
            broken = [f"{FROZEN_FILE} not found"] if stages is None else []
            stages = stages or {}
        out = {}
        try:
            now = self.current_hashes() if not broken else {}
        except ApiError as e:
            broken = [e.message]
        for name in STAGES:
            st = stages.get(name)
            if broken:
                problems = list(broken)
            elif st is None:
                problems = [f"stage {name} is not frozen"]
            else:
                problems = self._stage_problems(name, st["hashes"], now)
            out[name] = stage(problems, st)
        if out["rule"]["frozen"] and not out["conditions"]["valid"]:
            out["rule"]["problems"] = [*dict.fromkeys([*out["rule"]["problems"], *out["conditions"]["problems"]])]
            out["rule"]["valid"] = False
        rule = out["rule"]
        return {
            "frozen": rule["frozen"],
            "frozen_at": rule["frozen_at"],
            "valid": rule["valid"],
            "problems": rule["problems"],
            "stages": out,
        }

    def check_dev_open(self) -> None:
        """Dev seeds need a valid stage `conditions` (DECISIONS-11 §2.5)."""
        st = self.freeze_status()["stages"]["conditions"]
        if not st["valid"]:
            raise ApiError(409, "condition_not_frozen", "Dev seeds need a valid conditions freeze: " + "; ".join(st["problems"]))

    def check_test_open(self) -> None:
        """The one test gate (CR-17 U3-2): test seeds and test scenarios need a valid stage `rule`. 409 names what is wrong."""
        status = self.freeze_status()
        if not status["valid"]:
            raise ApiError(
                409,
                "test_split_locked",
                "Test seeds are locked until a valid rule freeze: " + "; ".join(status["problems"]),
            )

    def check_scenario_open(self, scenario_id: str | None) -> None:
        """An exploratory run or branch on a test seed's scenario waits for the freeze too (CR-17 U3-6), also on a copy of
        one under another id or name (the same market content; CR-26 m3). A changed number is a new market."""
        path = self.dir / SEEDS_FILE
        if scenario_id is None or not path.exists():
            return
        tests = [e.scenario_id for e in self.seed_map().seeds if e.split == "test"]
        if scenario_id in tests:
            self.check_test_open()
            return
        try:
            market = _market(self.catalog.scenario(scenario_id))
        except ApiError:
            return
        if any(_market(self.catalog.scenario(t)) == market for t in tests):
            self.check_test_open()

    def check_feature_code(self) -> None:
        """The server computes features with the code it loaded at start (CR-26 m1). A feature file edited since then gives
        a hash that no export matches, so the freeze and the export wait for a restart."""
        if FEATURES.code_hash() != LOADED_FEATURES_HASH:
            raise ApiError(409, "features_changed", "A feature module changed on disk after the server started. Restart the server.")

    def freeze(self, stage: str, approved_by: str, test_cells_used: int) -> dict:
        """POST /freeze (DECISIONS-11 §1–2). Never commits. Refused once a test cell is used: nothing can change after a
        test run. Stage conditions needs the judge rubric, checks every input, sets status frozen and frozen_hash in each
        condition file, and replaces frozen.json with this stage only (a rule stage must be frozen again). Stage rule needs
        a valid stage conditions and the decision rule, and adds its hash."""
        if test_cells_used:
            raise ApiError(409, "conflict", f"{test_cells_used} test cells are used; the freeze cannot change after a test run.")
        self.check_feature_code()
        if stage == "rule" and not self.freeze_status()["stages"]["conditions"]["valid"]:
            raise ApiError(409, "conflict", "Freeze stage conditions first; the rule stage keeps its condition hashes.")
        missing = [RESEARCH_FILES[k] for k in _stage_files(stage) if not (self.dir / RESEARCH_FILES[k]).exists()]
        if missing:
            raise ApiError(409, "freeze_incomplete", f"Stage {stage} needs conditions/{' and conditions/'.join(missing)}.")
        rule = self.dir / RESEARCH_FILES["decision_rule"]
        if stage == "rule" and PLACEHOLDER in rule.read_text(encoding="utf-8"):
            raise ApiError(409, "freeze_incomplete", f"conditions/{rule.name} holds the placeholder {PLACEHOLDER}; write the rule first.")
        if stage == "rule" and (problems := quality_in_rule(rule.read_text(encoding="utf-8"))):
            raise ApiError(409, "freeze_incomplete", f"conditions/{rule.name} uses a run-quality feature as a detector.", problems)
        if stage == "conditions":
            rubric = self.dir / RESEARCH_FILES["judge_rubric"]
            try:
                _, problems = itemsets.parse(yaml.safe_load(rubric.read_text(encoding="utf-8")))
            except yaml.YAMLError as e:
                problems = [{"path": "file", "message": f"not YAML: {e}"}]
            if problems:  # a frozen rubric is read only, so an invalid one would block every judge job (CR-37 m1)
                raise ApiError(409, "freeze_incomplete", f"conditions/{rubric.name} is not a valid item set.", problems)
            models = self.dir / RESEARCH_FILES["models"]
            try:
                file, problems = models_file.parse(yaml.safe_load(models.read_text(encoding="utf-8")))
            except yaml.YAMLError as e:
                file, problems = None, [{"path": "file", "message": f"not YAML: {e}"}]
            problems = problems if file is None else models_file.freeze_problems(file, self.conditions(), self._profile)
            if problems:
                raise ApiError(409, "freeze_incomplete", f"conditions/{models.name} cannot be frozen.", problems)
            if problems := setting_problems(self.conditions()):
                raise ApiError(
                    409,
                    "freeze_incomplete",
                    "The conditions differ in more than the channels they manipulate (DECISIONS-15 §3.4).",
                    problems,
                )
        for e in self.seed_map().seeds:
            scenario = self.catalog.scenario(e.scenario_id)
            if scenario.hash != e.scenario_hash:
                raise ApiError(
                    409, "hash_mismatch", f"Scenario {e.scenario_id} has hash {scenario.hash}, not {e.scenario_hash} as in the seed map."
                )
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if stage == "conditions":
            for c in self.conditions():
                path = self.dir / f"{c.id}.yaml"
                text = path.read_text(encoding="utf-8")
                text, n_status = re.subn(r"^status: \S+$", "status: frozen", text, flags=re.M)
                text, n_hash = re.subn(r"^frozen_hash: .*$", f'frozen_hash: "{c.hash}"', text, flags=re.M)
                if (n_status, n_hash) != (1, 1):
                    raise ApiError(500, "condition_invalid", f"Condition file {path.name} needs one status line and one frozen_hash line.")
                _write(path, text)
            stages = {}
        else:
            stages = self._stages()
        own = _stage_files(stage)
        hashes = {k: v for k, v in self.current_hashes().items() if k not in RESEARCH_FILES or k in own}
        stages[stage] = {"frozen_at": now, "approved_by": approved_by, "hashes": hashes}
        _write(self.dir / FROZEN_FILE, json.dumps({"stages": stages}, indent=2, sort_keys=True) + "\n")
        return self.freeze_status() | {"hashes": hashes}

    # Research config -> one config per seed

    def expand(self, config: RunConfig) -> list[SeedRun]:
        if config.condition_id is None:
            raise ApiError(
                422, "validation_error", "A research run needs a condition_id.", [{"path": "condition_id", "message": "required"}]
            )
        set_fields = [f for f in CONDITION_FIELDS if getattr(config, f) is not None]
        set_fields += [f"agents/{i}/capability" for i, a in enumerate(config.agents) if a.capability is not None]
        for i, a in enumerate(config.agents):
            if a.policy.llm is not None:
                set_fields += [
                    f"agents/{i}/policy/llm/{k}" for k in ("system_prompt", "talk_prompt") if getattr(a.policy.llm, k) is not None
                ]
        if set_fields:
            raise ApiError(
                422,
                "research_field_locked",
                "A research run takes these fields from its condition and seed map; leave them null.",
                [{"path": p, "message": "set by the condition"} for p in set_fields],
            )
        if config.split == "exploratory":
            raise ApiError(
                422, "validation_error", "A research run needs split pilot, dev or test.", [{"path": "split", "message": "not exploratory"}]
            )
        try:
            cond = self.condition(config.condition_id)
        except ApiError as e:
            if e.status != 404:
                raise
            raise ApiError(422, "validation_error", e.message, [{"path": "condition_id", "message": "unknown condition"}]) from None
        f = cond.file
        if config.split == "test":  # first: the gate names every changed file, not only this condition
            self.check_test_open()
        if f.frozen_hash is not None and f.frozen_hash != cond.hash:
            raise ApiError(
                409,
                "hash_mismatch",
                f"Condition {f.id} was frozen with hash {f.frozen_hash} but is now {cond.hash}. It changed after the freeze.",
            )
        if config.split == "dev":
            self.check_dev_open()
        if config.split == "dev" and f.status != "frozen":
            raise ApiError(409, "condition_not_frozen", f"Condition {f.id} is a draft. Dev seeds need a frozen condition; use pilot seeds.")
        if config.memory.summary is None:  # the condition decides (DECISIONS-15 §1); another value is refused below
            config = config.model_copy(update={"memory": config.memory.model_copy(update={"summary": f.memory_summary})})
        elif config.memory.summary != f.memory_summary:
            raise ApiError(
                422,
                "research_field_locked",
                f"Condition {f.id} sets memory.summary {f.memory_summary}.",
                [{"path": "memory/summary", "message": "set by the condition"}],
            )
        sellers = sorted(a.id for a in config.agents if a.role == "seller")
        if sellers != sorted(f.sellers):
            raise ApiError(422, "validation_error", f"Condition {f.id} has sellers {sorted(f.sellers)}; the config has {sellers}.")
        self._check_models(f, [a for a in config.agents if a.role == "seller"])
        if config.split in ("dev", "test"):
            self.check_models_frozen(f.id, config.agents)
        seeds = self.seed_map()
        wrong = [s for s in config.seeds if (e := seeds.entry(s)) is None or e.split != config.split]
        if wrong:
            raise ApiError(
                422,
                "seed_not_in_split",
                f"Seeds {wrong} are not {config.split} seeds in conditions/{SEEDS_FILE}.",
                [{"path": "seeds", "message": f"not in split {config.split}"}],
            )
        config_hash = config.config_hash()
        out = []
        for seed in config.seeds:
            entry = seeds.entry(seed)
            scenario = self.catalog.scenario(entry.scenario_id)
            if scenario.hash != entry.scenario_hash:
                raise ApiError(
                    409,
                    "hash_mismatch",
                    f"Scenario {entry.scenario_id} has hash {scenario.hash}, not {entry.scenario_hash} as in the seed map.",
                )
            variant = cond.variant(seed)
            per_seed = config.model_copy(
                update={
                    "scenario_id": scenario.id,
                    "scenario_hash": scenario.hash,
                    "prompt_variant_id": variant.id,
                    "seeds": [seed],
                    "channels": f.channels,
                    "transparency": f.transparency,
                    "call_mode": f.call_mode,
                    "observation_mode": f.observation_mode,
                    "limits": f.limits,
                    "agents": [_bind_agent(a, cond, variant) for a in config.agents],
                }
            )
            binding = ResearchBinding(f.id, cond.hash, f.labels, seeds.hash, f.c3_isolation, config_hash)
            out.append(SeedRun(per_seed, scenario, binding))
        return out

    def _profile(self, profile_id: str):
        if self.profile_of is None:
            raise ApiError(404, "not_found", f"Provider profile {profile_id} not found.")
        return self.profile_of(profile_id)

    def frozen_models(self) -> models_file.ModelsFile:
        """conditions/models.yaml; the dev and test gates run first, so it is the file of a valid conditions stage."""
        path = self.dir / RESEARCH_FILES["models"]
        file, problems = (None, [{"path": "file", "message": "missing"}])
        if path.exists():
            file, problems = models_file.parse(_yaml(path.read_text(encoding="utf-8")))
        if file is None:
            raise ApiError(409, "model_not_frozen", f"conditions/{path.name} is not a valid models file.", problems)
        return file

    def check_models_frozen(self, condition_id: str, agents: list[AgentConfig]) -> None:
        """Dev and test sellers use the models of conditions/models.yaml for their condition (CR-43 M1)."""
        errors = models_file.seller_differences(self.frozen_models(), condition_id, agents, self._profile)
        if errors:
            raise ApiError(
                409, "model_not_frozen", f"Dev and test runs of {condition_id} use the sellers of conditions/models.yaml.", errors
            )

    def check_judge_frozen(self, llm) -> None:
        """A dev or test LLM judge job uses the judge of conditions/models.yaml (CR-43 M1)."""
        errors = models_file.judge_differences(self.frozen_models(), llm, self._profile)
        if errors:
            raise ApiError(409, "model_not_frozen", "Dev and test judge jobs use the judge of conditions/models.yaml.", errors)

    @staticmethod
    def _check_models(f: ConditionFile, sellers: list[AgentConfig]) -> None:
        """C2a: every seller on the same model. C2b: every seller on a different model family. Scripted sellers count by kind."""
        if f.same_model_for_all_sellers is None:
            return
        if f.same_model_for_all_sellers:
            keys = {a.policy.llm.model if a.policy.llm else f"scripted:{a.policy.kind}" for a in sellers}
            ok = len(keys) == 1
        else:
            keys = [a.policy.llm.model_family if a.policy.llm else f"scripted:{a.policy.kind}" for a in sellers]
            ok = None not in keys and len(set(keys)) == len(keys)
        if not ok:
            rule = "the same model" if f.same_model_for_all_sellers else "a different model_family each"
            raise ApiError(
                422, "validation_error", f"Condition {f.id} needs {rule} for its sellers.", [{"path": "agents", "message": rule}]
            )


def _bind_agent(a: AgentConfig, cond: Condition, variant: PromptVariant) -> AgentConfig:
    if a.role != "seller":
        return a
    update: dict = {"capability": cond.capabilities[a.id]}
    if a.policy.llm is not None:
        p = variant.sellers[a.id]
        llm = a.policy.llm.model_copy(update={"system_prompt": p.system_prompt, "talk_prompt": p.talk_prompt})
        update["policy"] = a.policy.model_copy(update={"llm": llm})
    return a.model_copy(update=update)


def _market(scenario: Scenario) -> str:
    """The scenario content without its id, name, description and provenance: what makes two scenarios the same market."""
    return canonical_hash({k: v for k, v in scenario.content().items() if k not in ("id", "name", "description", "provenance")})


def _text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _yaml(text: str):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ApiError(422, "validation_error", f"The text is not valid YAML: {e}", [{"path": "text", "message": "not YAML"}]) from None


def _write(path: Path, text: str) -> None:
    """Write through a temporary file, so a crash leaves the old file or the new one, never half of one."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
