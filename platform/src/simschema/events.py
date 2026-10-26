"""SimEvent types: the lines of episode.jsonl (schema episode.v5; v4 files still load) and the WebSocket payload. One source for both."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field, TypeAdapter, model_validator

from simschema.base import Frozen

SCHEMA_VERSION = "episode.v5"
Tier = Literal["A", "B", "C"]
CallPurpose = Literal["talk", "act", "one_call", "summary"]
FallbackReason = Literal["timeout", "error", "invalid", "max_steps"]
"""Why the system set the action or the PASS (DECISIONS-22 entry 2): two timeouts; the attempts ended on a provider error;
they ended on an invalid reply (invalid_tool, parse_error); or the steps of the call ended with no action."""
RunStatus = Literal["queued", "running", "paused", "stopping", "completed", "stopped", "failed", "recovering"]
EndReason = Literal["horizon", "stopped_by_user", "budget_exceeded", "rate_window", "error"]


class Labels(Frozen):
    legal: Literal["yes", "no", "undetermined"]
    harm: Literal["yes", "no"]


class ToolGrant(Frozen):
    tool_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class Capability(Frozen):
    """A set of tool grants. The tool registry checks each grant against the agent's role."""

    grants: list[ToolGrant] = Field(default_factory=list)
    max_queries_per_round: int = Field(default=3, ge=0, le=3)

    def grant(self, tool_id: str) -> ToolGrant | None:
        return next((g for g in self.grants if g.tool_id == tool_id), None)


class Reference(Frozen):
    """The model reference values for one round's params and sellers (simcore.market.Market.reference)."""

    nash: float
    joint_optimum: float
    nash_by_seller: dict[str, float]
    joint_by_seller: dict[str, float]
    nash_profit: float
    joint_profit: float


class ReferencePoint(Reference):
    t: int
    params: dict[str, Any] = Field(description="The demand params in force this round (shape set by the demand model)")
    costs: dict[str, float] = Field(description="Marginal cost of each seller this round")


class ProbeIntervention(Frozen):
    type: Literal["probe"] = "probe"
    target: str
    price: float | Literal["nash"] = "nash"
    rounds: int = Field(default=1, ge=1)


class PlaceboIntervention(Frozen):
    type: Literal["placebo"] = "placebo"


class AblationIntervention(Frozen):
    """ablated channel: one channel's messages are cleared or shuffled from the fork. ablated none (a null ablation, for a
    group with no private channel): nothing is removed, the child only draws fresh samples, so its effect is the noise floor.
    A real ablation child replays the parent until the first changed message; a null one never does, so the C2 noise
    floor is wider than the noise in C1 and C3."""

    type: Literal["ablation"] = "ablation"
    ablated: Literal["channel", "none"] = "channel"
    channel_id: str | None = None
    mode: Literal["clear", "shuffle"] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "AblationIntervention":
        channel = self.ablated == "channel"
        if (self.channel_id is not None) != channel or (self.mode is not None) != channel:
            raise ValueError("ablated channel sets channel_id and mode; ablated none sets neither")
        return self


Intervention = Annotated[Union[ProbeIntervention, PlaceboIntervention, AblationIntervention], Field(discriminator="type")]


class BranchInfo(Frozen):
    parent_run_id: str
    fork_t: int
    intervention: Intervention
    sample_index: int = 0


class ChannelInfo(Frozen):
    id: str
    kind: Literal["public", "private"]
    participants: list[str]


LimitName = Literal[
    "max_steps_per_turn", "max_messages_per_round", "post.max_per_round", "max_queries_per_round", "capability.max_queries_per_round"
]


class RunLimits(Frozen):
    """Per-turn and per-round limits of a run (DECISIONS-8 §2). A research run takes them from its condition."""

    max_steps_per_turn: int = Field(default=5, ge=1, le=20, title="Max steps per turn", description="Model calls in one call phase")
    max_messages_per_round: int = Field(
        default=1, ge=0, le=5, title="Max messages per round", description="Posts per seller per round; the post grant can lower it"
    )
    max_queries_per_round: int = Field(
        default=3,
        ge=0,
        le=3,
        title="Max queries per round",
        description="Read-tool calls per seller per round; the capability can lower it",
    )

    def messages(self, grant_max: int) -> tuple[int, LimitName]:
        """The binding message limit and its name: the smaller of this limit and the post grant's max_per_round."""
        return (
            (self.max_messages_per_round, "max_messages_per_round")
            if self.max_messages_per_round <= grant_max
            else (grant_max, "post.max_per_round")
        )

    def queries(self, cap_max: int) -> tuple[int, LimitName]:
        return (
            (self.max_queries_per_round, "max_queries_per_round")
            if self.max_queries_per_round <= cap_max
            else (cap_max, "capability.max_queries_per_round")
        )


class PriceInfo(Frozen):
    """The provider price frozen on the run when it starts. source unknown: calls record cost_usd null."""

    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cached_input_usd_per_mtok: float | None = None
    source: Literal["user", "unknown"]
    as_of: str


class AgentHeader(Frozen):
    id: str
    role: str = "seller"
    policy_kind: str
    capability: Capability
    provider_profile_id: str | None = None
    base_url_host: str | None = None
    protocol: str | None = None
    base_path: str | None = Field(default=None, description="Path of the profile's base URL, without query (never a key)")
    model: str | None = None
    temperature: float | None = None
    prompt_hash: str | None = None
    price: PriceInfo | None = None


class ClockInfo(Frozen):
    mode: Literal["sync", "event"] = "sync"


class ScenarioRef(Frozen):
    id: str
    hash: str
    demand_model: str
    demand_version: str


class EpisodeHeader(Frozen):
    schema_version: Literal["episode.v4", "episode.v5"] = Field(
        default=SCHEMA_VERSION, description="v5 adds talk_replied, talk_text and two Observation fields; v4 files still load"
    )
    run_id: str
    batch_id: str | None = None
    kind: Literal["research", "exploratory"]
    condition_id: str | None = None
    condition_hash: str | None = None
    labels: Labels | None = None
    parent_labels: Labels | None = Field(
        default=None,
        description="Ablation children only: the parent's labels. Such a child is dependence evidence, never a member of a case group",
    )
    label_source: Literal["condition_config"] | None = None
    split: Literal["pilot", "dev", "test", "exploratory"]
    seed: int
    seeds_hash: str | None = None
    scenario: ScenarioRef
    branch: BranchInfo | None = None
    agents: list[AgentHeader]
    seller_order: list[str]
    channels: list[ChannelInfo]
    call_mode: Literal["one_call", "talk_and_act"]
    c3_isolation: bool = False
    memory_summary: Literal["code", "llm", "none"] = "code"
    observation_mode: Literal["push", "pull", "both"] | None = Field(
        default=None,
        description="push: each observation holds the visible record and no call offers a read of the record; pull: the"
        " observation holds the round and the own last result, and the seller reads with its read tools; both"
        " (DECISIONS-15 §3.2). Null in episodes stored before the field: those ran as both",
    )
    clock: ClockInfo = ClockInfo()
    limits: RunLimits | None = None
    max_rounds: int
    demand: Literal["formula", "agent"] = "formula"
    reproducible: bool = True
    preset_id: str | None = None
    config_hash: str | None = None
    cloned_from: str | None = None
    simcore_version: str
    code_commit: str | None = None
    created_at: str


class Own(Frozen):
    last_price: float | None = None
    last_quantity: float | None = None
    last_profit: float | None = None
    last_price_set_by_system: bool = False


class NewMessage(Frozen):
    channel_id: str
    sender: str
    text: str


class NewsItem(Frozen):
    t: int
    text: str


class OwnRound(Frozen):
    t: int
    price: float
    quantity: float
    profit: float
    set_by_system: bool


class PricePoint(Frozen):
    t: int
    price: float


class RecentMessage(Frozen):
    channel_id: str
    t: int = Field(description="Round it was posted")
    deliver_t: int
    sender: str
    text: str


class Observation(Frozen):
    t: int
    own: Own
    new_message_count: int
    headlines: list[str]
    masked: list[str] = Field(default_factory=list)
    rival_last_prices: dict[str, float] | None = Field(
        default=None,
        description="Each rival's price last round; only with the get_rival_prices grant. Null in episodes before 0.18.0",
    )
    new_messages: list[NewMessage] | None = Field(
        default=None, description="The messages from other agents delivered this round. Null in episodes before 0.18.0"
    )
    news: list[NewsItem] | None = Field(
        default=None,
        description="Every news item the agent may see, from round 1 (DECISIONS-15 §1; the last 5 before it)."
        " Empty without the get_news grant. Null before 0.23.0",
    )
    recent_messages: list[RecentMessage] | None = Field(
        default=None,
        description="Every message delivered so far on the channels this call may read, from round 1, own included, after"
        " masks (none in a C3 act call; DECISIONS-15 §1, the last 6 before it). Null before 0.23.0",
    )
    own_history: list[OwnRound] | None = Field(
        default=None,
        description="The agent's price, quantity and profit for every round so far, the last one included (DECISIONS-15"
        " §1; the last 5 before it). Empty without the get_own_sales grant. Null before 0.23.0",
    )
    mask_noop: bool | None = Field(
        default=None,
        description="True when a shuffle mask is active on a channel this call reads but the other senders' messages"
        " visible there have fewer than 2 distinct texts, so the mask changed nothing. Null before 0.23.0",
    )
    rival_history: dict[str, list[PricePoint]] | None = Field(
        default=None,
        description="Each rival's price for every round so far, only with the get_rival_prices grant (DECISIONS-15 §1)."
        " Null in episodes stored before it",
    )


class EventBase(Frozen):
    seq: int = Field(ge=0)
    run_id: str
    t: float
    wall_time: str
    tier: Tier


class RunStartedEvent(EventBase):
    type: Literal["run_started"] = "run_started"
    header: EpisodeHeader


class RoundStartedEvent(EventBase):
    type: Literal["round_started"] = "round_started"
    round: int
    reference: ReferencePoint


class NewsPublishedEvent(EventBase):
    type: Literal["news_published"] = "news_published"
    event_index: int
    text: str
    visible: list[str]


class ObservedEvent(EventBase):
    type: Literal["observed"] = "observed"
    agent_id: str
    purpose: CallPurpose
    observation: Observation


class LlmCallEvent(EventBase):
    type: Literal["llm_call"] = "llm_call"
    agent_id: str
    purpose: CallPurpose
    request_hash: str
    sample_index: int = 0
    cache_hit: bool
    charged: bool = Field(default=False, description="A cache hit this run paid for in a lost round, counted again once")
    attempt: int = Field(ge=1, le=3)
    provider_profile_id: str
    model: str
    input_tokens: int | None = Field(default=None, description="Cached input tokens included")
    output_tokens: int | None = Field(default=None, description="Reasoning tokens included")
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = Field(default=None, description="null when the profile has no price")
    latency_ms: int | None = None
    error: Literal["parse_error", "invalid_tool", "timeout", "rate_limit", "auth", "not_found", "provider_error"] | None = None
    http_status: int | None = Field(default=None, description="HTTP status of a failed request, when there was one (0.22.1)")
    provider_code: str | None = Field(
        default=None,
        max_length=48,
        description="The provider's error code from the error body, or the exception class of a failure with no HTTP status (0.22.1)",
    )
    degraded: bool | None = Field(
        default=None,
        description="True: sent with the model's thinking_on_timeout after a timeout of this call (DECISIONS-22 entry 5.2). Null otherwise",
    )
    slot_wait_ms: int | None = Field(default=None, description="Wait for a concurrency slot of the profile before the request (D22 5.8)")
    first_byte_ms: int | None = Field(
        default=None, description="From the request to the first parsed stream event (not an SSE comment); null with none (D22 5.8)"
    )
    provider_message: str | None = Field(
        default=None,
        max_length=200,
        description="The provider's error message of a failed request, redacted (runs of 16 or more key-like characters removed)"
        " and cut to 200 characters (DECISIONS-24 §3). Null on success",
    )
    parallel: bool | None = Field(
        default=None,
        description="True: called at the same time as the other LLM sellers of its phase; false: one after the other, as the run"
        " budget had no room for every worst case (DECISIONS-22 entry 5.6). Null: one LLM seller, or no seller phase",
    )
    settings: dict[str, Any] | None = Field(
        default=None,
        description="The request settings as sent (DECISIONS-23 entries 3 and 4): a configured setting that the request does not"
        " carry is left out, and sent_reasoning holds the provider's reasoning fields in place of thinking. The request_hash"
        " covers the configured settings. Null in events stored before this field",
    )


class ToolCalledEvent(EventBase):
    type: Literal["tool_called"] = "tool_called"
    agent_id: str
    tool: str
    tool_version: str | None = None
    arguments: dict[str, Any]
    allowed: bool
    result: Any = None
    result_hash: str | None = None


class PostedEvent(EventBase):
    type: Literal["posted"] = "posted"
    agent_id: str
    channel_id: str
    text: str
    deliver_t: float
    receivers: list[str] | None = Field(
        default=None,
        description="The agents other than the sender that the message is delivered to (their observed event in round deliver_t "
        "lists it), in channel order; [] for a post in the last round. Under a shuffle mask the text each one saw is in its observed "
        "event. Null in episodes written before contract 0.21.0, and then left out of the episode hash",
    )


class TalkRepliedEvent(EventBase):
    """How one LLM talk turn ended (PILOT-1c). Tier A holds no text that was not posted (DECISIONS-12 §2a)."""

    type: Literal["talk_replied"] = "talk_replied"
    agent_id: str
    channel_id: str | None = Field(default=None, description="Where a plain-text reply is posted; null when the turn uses the post tool")
    outcome: Literal["posted", "pass", "empty", "refused", "error", "max_steps"]
    text: str | None = Field(default=None, description="The posted text when outcome is posted, else null")
    reason: FallbackReason | None = Field(
        default=None,
        description="timeout: a PASS the system recorded after two timeouts (DECISIONS-22 entry 2). Null: the model's own outcome",
    )


class TalkTextEvent(EventBase):
    """Tier B. Reply text of a talk call that was not posted: PASS, an over-limit reply, or the text beside a post call."""

    type: Literal["talk_text"] = "talk_text"
    agent_id: str
    text: str


class ActedEvent(EventBase):
    type: Literal["acted"] = "acted"
    agent_id: str
    price: float
    hold: bool
    fallback: bool = False
    fallback_reason: FallbackReason | None = Field(default=None, description="With fallback true (DECISIONS-22 entry 2); null before 0.34")
    forced: bool = False
    asked_price: float | None = Field(
        default=None,
        description="The price the seller asked with set_price when the grid snap changed it (off the grid or past a bound); "
        "price is the snapped value. Null when the asked price was on the grid, and in episodes written before contract 0.41.0 "
        "(DECISIONS-30 §3.3)",
    )
    steps: int | None = Field(default=None, description="Model calls this round (LLM agents)")
    tool_calls: int | None = None
    messages: int | None = None
    limits_hit: list[LimitName] = Field(default_factory=list)


class PlannedEvent(EventBase):
    """The private plan text a model gave with set_price or hold, or the text of a write_note call (source note). Model-written,
    so tier C: never a feature or judge input."""

    type: Literal["planned"] = "planned"
    agent_id: str
    plan: str
    source: Literal["note"] | None = Field(default=None, description="note: the text of a write_note call; null: a plan")


class SettledEvent(EventBase):
    """What a log auditor sees for one round: prices, own quantities, own profits (tier A)."""

    type: Literal["settled"] = "settled"
    prices: dict[str, float]
    quantities: dict[str, float]
    profits: dict[str, float]
    clamped: list[str] = Field(default_factory=list)


class ReferenceEvent(EventBase):
    """Values that need the payoff model for the settled round at the same t: true params, Nash, joint optimum,
    best responses, consumer surplus (tier B). A logs-only reader never has these."""

    type: Literal["reference"] = "reference"
    reference: ReferencePoint
    best_response: dict[str, float]
    consumer_surplus: float


class OutcomeEvent(EventBase):
    """Generic per-round outcome for environments that are not this market (for example a workflow task).
    The market does not emit it. `values` maps a name to a number; `units` gives each name's unit. Tier A: only
    values that a log shows go here (model-only values belong in a tier-B event)."""

    type: Literal["outcome"] = "outcome"
    agent_id: str | None = None
    values: dict[str, float]
    units: dict[str, str] = Field(default_factory=dict)


class MemorySummarisedEvent(EventBase):
    type: Literal["memory_summarised"] = "memory_summarised"
    agent_id: str
    summary: str
    covers: tuple[float, float]


class IntervenedEvent(EventBase):
    type: Literal["intervened"] = "intervened"
    intervention: Intervention
    effective_price: float | None = None


class SnapshotSavedEvent(EventBase):
    type: Literal["snapshot_saved"] = "snapshot_saved"
    snapshot_hash: str


class BudgetWarningEvent(EventBase):
    type: Literal["budget_warning"] = "budget_warning"
    spent_usd: float
    max_usd: float
    level: Literal["p80", "p100"]


class RunStatusChangedEvent(EventBase):
    type: Literal["run_status_changed"] = "run_status_changed"
    status: RunStatus
    reason: str | None = None


class ErrorWhere(Frozen):
    phase: Literal["talk", "act", "settle"]
    seller_id: str
    t: int


class ErrorDetail(Frozen):
    """The most specific cause the server has of a failed run (UX-omp-review-2 item 2). Text is redacted and cut."""

    code: str = Field(description="As run_ended.error: the error code, or the exception class")
    exception: str = Field(description="The exception class")
    where: ErrorWhere | None = Field(default=None, description="The seller call that failed. Null: the failure was outside one")
    message: str | None = Field(
        default=None, max_length=200, description="The exception text, masked (key-shaped strings; model ids stay), cut to 200 characters"
    )
    http_status: int | None = None
    provider_code: str | None = Field(default=None, max_length=48)
    provider_message: str | None = Field(default=None, max_length=200)


class RunEndedEvent(EventBase):
    type: Literal["run_ended"] = "run_ended"
    status: Literal["completed", "stopped", "failed"]
    end_reason: EndReason
    episode_hash: str
    cost_usd: float = 0.0
    error: str | None = None
    error_detail: ErrorDetail | None = Field(default=None, description="Set on a failed run (end_reason error), null otherwise (0.38.0)")


SimEvent = Annotated[
    Union[
        RunStartedEvent,
        RoundStartedEvent,
        NewsPublishedEvent,
        ObservedEvent,
        LlmCallEvent,
        ToolCalledEvent,
        PostedEvent,
        TalkRepliedEvent,
        TalkTextEvent,
        ActedEvent,
        PlannedEvent,
        SettledEvent,
        ReferenceEvent,
        OutcomeEvent,
        MemorySummarisedEvent,
        IntervenedEvent,
        SnapshotSavedEvent,
        BudgetWarningEvent,
        RunStatusChangedEvent,
        RunEndedEvent,
    ],
    Field(discriminator="type"),
]
SIM_EVENT = TypeAdapter(SimEvent)

TIER: dict[str, Tier] = {
    "run_started": "B",
    "round_started": "B",
    "news_published": "A",
    "observed": "A",
    "llm_call": "B",
    "tool_called": "A",
    "posted": "A",
    "talk_replied": "A",
    "talk_text": "B",
    "acted": "A",
    "planned": "C",
    "settled": "A",
    "reference": "B",
    "outcome": "A",
    "memory_summarised": "C",
    "intervened": "B",
    "snapshot_saved": "B",
    "budget_warning": "B",
    "run_status_changed": "B",
    "run_ended": "B",
}
