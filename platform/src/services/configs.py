"""RunConfig (the contract's request body) -> one RunSpec per seed. Policies are built from the same config."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from simcore.agents import AgentPolicy, PolicyContext
from simcore.callplan import CallPlan, Setting, call_plan, open_channels
from simcore.run import RunSpec
from simcore.scenario import Scenario
from simcore.scripted import SCRIPTED, make_scripted
from simschema.base import field_title
from simschema.events import AgentHeader, Capability, RunLimits


class ApiError(Exception):
    """An error with a contract `code`. The message is for the user and never holds a key or a request body."""

    def __init__(self, status: int, code: str, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.errors = status, code, message, errors or []


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", field_title_generator=field_title)


class FieldError(ValueError):
    """A model-validator error about one field. The API reports `field` (under the model's own path) as the error path."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class LlmSettings(Model):
    provider_profile_id: str = Field(
        min_length=1, title="Provider profile", description="A profile from GET /provider-profiles; never a key"
    )
    model: str = Field(min_length=1)
    temperature: float = Field(0.7, ge=0, le=2)
    top_p: float | None = Field(None, gt=0, le=1)
    max_output_tokens: int | None = Field(
        None,
        ge=1,
        description="Per call; reasoning tokens count as output. Null (the default): not sent, the provider's maximum"
        " (DECISIONS-18 entry 2). A stored config leaves nulls out, so the default must be null for null to survive",
    )
    timeout_s: float = Field(60, gt=0, le=600, description="Deadline of the whole call, in seconds (DECISIONS-18 entry 2, DECISIONS-21)")
    thinking: bool | Literal["minimal", "low", "medium", "high", "xhigh"] | None = Field(
        None,
        description="Pydantic AI's unified thinking setting, which maps it to the provider's own field (DECISIONS-18 entry 3)."
        " It sets the effort only; returned thinking is kept as before. Null: not sent, the provider default",
    )
    thinking_on_timeout: bool | Literal["minimal", "low", "medium", "high", "xhigh"] | None = Field(
        None,
        description="The thinking setting of the attempt after a timeout (DECISIONS-22 entry 5.3; the call is then degraded). Null:"
        " the retry keeps `thinking`",
    )
    system_prompt: str | None = Field(None, description="Pricing prompt; null uses the default base prompt")
    talk_prompt: str | None = Field(None, description="System prompt of the talk call; null uses system_prompt")
    model_family: str | None = Field(None, description="C2b needs a different family per seller")


class PolicySettings(Model):
    kind: str
    llm: LlmSettings | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _llm_settings(self) -> "PolicySettings":
        if self.kind == "llm" and self.llm is None:
            raise FieldError("llm", "policy kind llm needs llm settings")
        return self


class AgentConfig(Model):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,15}$")
    role: str
    display_name: str | None = None
    policy: PolicySettings
    capability: Capability | None = None


class ChannelSettings(Model):
    public: bool = False
    private_pairs: bool = False
    news: bool = True


class ClockSettings(Model):
    mode: Literal["sync", "event"]
    wake_min: int = 1
    wake_max: int = 5


class MemorySettings(Model):
    full_rounds: int = Field(
        5, description="Not used: each call shows the whole record (DECISIONS-15 §1). Kept so older configs stay valid"
    )
    summary_every: int = Field(5, ge=1, description="Rounds between memory notes")
    summary: Literal["code", "llm", "none"] | None = Field(
        None,
        description="code: a fixed-format note; llm: the model writes the note; none: no notes (research conditions,"
        " DECISIONS-15 §1: the observation holds the whole record). Null: the condition's value in a research run, code"
        " in an exploratory run",
    )


class BudgetSettings(Model):
    """Per-run caps. At least one is set. A call with no price is not seen by max_usd; max_calls and max_tokens still see it."""

    max_usd: float | None = Field(None, gt=0, title="Max USD", description="Stops the run before a call that could pass this cost")
    max_calls: int | None = Field(None, ge=1, description="Paid model calls of the run")
    max_tokens: int | None = Field(None, ge=1, description="Input + output tokens of the run's paid calls")

    @model_validator(mode="after")
    def _one(self) -> "BudgetSettings":
        if self.max_usd is None and self.max_calls is None and self.max_tokens is None:
            raise FieldError("max_usd", "set at least one of max_usd, max_calls and max_tokens")
        return self


class BatchBudget(Model):
    """Shared by every run of a batch: all seeds of one POST /runs, or all runs of an experiment."""

    max_usd: float | None = Field(None, gt=0, title="Max USD", description="For all runs together")
    max_calls: int | None = Field(None, ge=1, description="Paid model calls of all runs together")
    max_tokens: int | None = Field(None, ge=1, description="Input + output tokens of all runs together")

    @model_validator(mode="after")
    def _one(self) -> "BatchBudget":
        if self.max_usd is None and self.max_calls is None and self.max_tokens is None:
            raise FieldError("max_usd", "a batch budget needs max_usd, max_calls or max_tokens")
        return self


class IndicatorRef(Model):
    id: str
    params: dict[str, Any] = Field(default_factory=dict)


class RunConfig(Model):
    kind: Literal["research", "exploratory"]
    name: str | None = None
    scenario_id: str | None = None
    scenario_hash: str | None = None
    condition_id: str | None = None
    prompt_variant_id: str | None = None
    split: Literal["pilot", "dev", "test", "exploratory"] = "exploratory"
    seeds: list[int] = Field(min_length=1)
    agents: list[AgentConfig] = Field(min_length=2)
    channels: ChannelSettings | None = None
    transparency: Literal["rival_last_round", "opaque"] | None = None
    call_mode: Literal["one_call", "talk_and_act"] | None = None
    observation_mode: Literal["push", "pull", "both"] | None = Field(
        None,
        description="push: the whole visible record in each call, no read of the record offered; pull: only the round and the"
        " own last result, the agent reads with its read tools (needs a query limit above 0); both. Null: both. A research"
        " run takes it from its condition",
    )
    clock: ClockSettings
    memory: MemorySettings = Field(default_factory=MemorySettings)
    limits: RunLimits | None = Field(
        None, description="Per-turn and per-round limits; null uses the defaults. A research run takes them from its condition"
    )
    budget: BudgetSettings
    batch_budget: BatchBudget | None = Field(
        None, description="Shared by every seed of this request (its batch). Branches do not inherit it; experiments have their own"
    )
    snapshot_every: int = 1
    notes: str | None = None
    preset_id: str | None = None
    indicators: list[IndicatorRef] = Field(default_factory=list)
    opening_price: float | None = Field(
        None,
        ge=0,
        title="Current market price at opening",
        description="The market price when trading opens, stated once in every seller's market facts. A reference only: not a "
        "cap and not a grid bound, and sellers price freely on the grid. Null: not stated (Raymond via Grok, 24 Oct)",
    )
    replicate: int = Field(
        0,
        ge=0,
        description="Replicate number of a rerun: 0 is the first run. It moves the call-cache key (sample index replicate * 1,000,000"
        " + branch sample index), so a rerun of the same condition and seed makes new calls instead of replaying the cache."
        " 0 is left out of the config hash (DECISIONS-36 §5)",
    )

    @model_validator(mode="after")
    def _exploratory(self) -> "RunConfig":
        """A research config leaves these to its condition and seed map (services.research checks that)."""
        if self.kind != "exploratory":
            return self
        if self.memory.summary is None:
            self.memory = self.memory.model_copy(update={"summary": "code"})
        for f in ("scenario_id", "scenario_hash", "call_mode"):
            if getattr(self, f) is None:
                raise FieldError(f, "an exploratory run needs scenario_id, scenario_hash and call_mode")
        for f in ("condition_id", "prompt_variant_id"):
            if getattr(self, f) is not None:
                raise FieldError(f, "an exploratory run has no condition_id and no prompt_variant_id")
        return self

    def config_hash(self) -> str:
        """Hash of the config without its seeds: a clone with another seed keeps the hash."""
        drop = {"seeds", "name", "notes", "batch_budget"} | ({"opening_price"} if self.opening_price is None else set())
        drop |= {"replicate"} if self.replicate == 0 else set()
        body = self.model_dump(mode="json", exclude=drop)  # a null opening_price keeps the hash of configs stored before it
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


LlmBinder = Callable[["AgentConfig"], dict]
"""Checks an LLM agent's profile (key set, protocol built) and returns its AgentHeader fields (profile, wire, model, price, prompt hash)."""
LlmFactory = Callable[["AgentConfig", AgentHeader, Any], AgentPolicy]
"""Builds the LLM policy of one agent for one run, given its header and the run's recorder."""


def build_specs(
    config: RunConfig,
    scenario: Scenario,
    batch_id: str,
    run_ids: list[str],
    created_at: str,
    bind: LlmBinder | None = None,
    research: Any = None,
) -> list[RunSpec]:
    """`research`: the services.research.ResearchBinding of a per-seed research config (labels and provenance)."""
    if config.kind == "research" and not scenario.market.demand.spec.research_allowed:
        raise ApiError(
            422,
            "demand_not_research_allowed",
            f"Demand model {scenario.market.demand.id} has no closed-form references, so research runs cannot use it.",
        )
    if (config.kind == "research") != (research is not None):
        raise ValueError("a research config is built only after services.research.expand")
    if config.clock.mode != "sync":
        raise ApiError(
            422,
            "validation_error",
            "Event clock mode is designed, not built. Use clock.mode sync.",
            [{"path": "clock/mode", "message": "not built"}],
        )
    if config.scenario_hash != scenario.hash:
        raise ApiError(409, "hash_mismatch", f"Scenario {scenario.id} has hash {scenario.hash}, not {config.scenario_hash}.")
    channels = config.channels or ChannelSettings()
    plan = call_plan_of(config, research.c3_isolation if research else False)
    agents = []
    for i, a in enumerate(config.agents):
        if a.capability is None and a.role != "host":
            raise ApiError(
                422,
                "validation_error",
                f"Agent {a.id} needs a capability.",
                [{"path": f"agents/{i}/capability", "message": "required"}],
            )
        extra: dict = {}
        if a.policy.kind == "llm":
            if bind is None:
                raise ApiError(422, "profile_key_missing", "This server has no provider profiles configured for LLM agents.")
            extra = bind(a)
        elif a.policy.kind not in SCRIPTED and a.policy.kind != "scripted_host":
            raise ApiError(
                422,
                "validation_error",
                f"Unknown policy kind {a.policy.kind}.",
                [{"path": f"agents/{i}/policy/kind", "message": "unknown"}],
            )
        capability = plan.sellers[a.id].capability if a.id in plan.sellers else a.capability or Capability()
        agents.append(AgentHeader(id=a.id, role=a.role, policy_kind=a.policy.kind, capability=capability, **extra))
    if plan.refused:
        raise ApiError(422, _code(plan.refused[0]), plan.refused[0])
    specs = []
    for seed, run_id in zip(config.seeds, run_ids):
        try:
            specs.append(
                RunSpec(
                    run_id=run_id,
                    batch_id=batch_id,
                    kind=config.kind,
                    split=config.split,
                    seed=seed,
                    agents=agents,
                    public=channels.public,
                    private_pairs=channels.private_pairs,
                    call_mode=config.call_mode,
                    memory_summary=config.memory.summary,
                    observation_mode=config.observation_mode or "both",
                    limits=config.limits or RunLimits(),
                    preset_id=config.preset_id,
                    config_hash=research.config_hash if research else config.config_hash(),
                    created_at=created_at,
                    **(
                        {
                            "condition_id": research.condition_id,
                            "condition_hash": research.condition_hash,
                            "labels": research.labels,
                            "seeds_hash": research.seeds_hash,
                            "c3_isolation": research.c3_isolation,
                        }
                        if research
                        else {}
                    ),
                )
            )
        except ValidationError as e:
            msg = e.errors()[0]["msg"].removeprefix("Value error, ")
            raise ApiError(422, _code(msg), msg) from None
    sellers = sorted(a.id for a in agents if a.role == "seller")
    if sellers != sorted(scenario.seller_ids):
        raise ApiError(422, "validation_error", f"Scenario {scenario.id} has sellers {scenario.seller_ids}; the config has {sellers}.")
    return specs


def is_open(channel_id: str, channels: ChannelSettings) -> bool:
    return channels.public if channel_id == "public" else channels.private_pairs


def call_plan_of(config: RunConfig, c3_isolation: bool = False) -> CallPlan:
    """The config's call plan (simcore.callplan): the run switches win over a grant (DECISIONS-15 §3.1; CR-69 1), so the
    header records each seller's effective grants."""
    channels = config.channels or ChannelSettings()
    sellers = {a.id: a.capability or Capability() for a in config.agents if a.role == "seller"}
    return call_plan(
        Setting(
            capabilities=sellers,
            channels=open_channels(list(sellers), channels.public, channels.private_pairs),
            call_mode=config.call_mode,
            c3_isolation=c3_isolation,
            observation_mode=config.observation_mode or "both",
            limits=config.limits or RunLimits(),
            memory_summary=config.memory.summary,
            news=channels.news,
            transparency=config.transparency,
        )
    )


def _code(text: str) -> str:
    for needle, code in (
        ("not allowed for role", "tool_not_allowed_for_role"),
        ("unknown tool", "unknown_tool"),
        ("cannot take part", "role_not_built"),
        ("not allowed in a research run", "role_not_allowed_in_research"),
        ("granted twice", "invalid_grant_params"),
        ("grant ", "invalid_grant_params"),
    ):
        if needle in text:
            return code
    return "validation_error"


def check_scripted(config: RunConfig, scenario: Scenario) -> None:
    """Build every scripted seller once, so bad params fail at the start (and the estimate), not in the run."""
    scripted = [a for a in config.agents if a.policy.kind != "llm"]
    make_policies(config.model_copy(update={"agents": scripted}), scenario)


def make_policies(
    config: RunConfig, scenario: Scenario, spec: RunSpec | None = None, recorder: Any = None, llm: LlmFactory | None = None
) -> dict[str, AgentPolicy]:
    market = scenario.market.build()
    sellers = tuple(sorted(a.id for a in config.agents if a.role == "seller"))
    out = {}
    for a in config.agents:
        if a.role != "seller":
            continue
        if a.policy.kind == "llm":
            if llm is None or spec is None:
                raise ApiError(422, "profile_key_missing", f"Agent {a.id} is an LLM agent; this run has no LLM context.")
            out[a.id] = llm(a, next(h for h in spec.agents if h.id == a.id), recorder)
            continue
        ctx = PolicyContext(seller_id=a.id, sellers=sellers, market=market, params=scenario.params)
        try:
            out[a.id] = make_scripted(a.policy.kind, ctx, **a.policy.params)
        except TypeError as e:
            raise ApiError(422, "validation_error", f"Agent {a.id}: bad policy params ({e}).") from None
    return out
