"""Tool registry. A capability is a list of grants; each grant names a registered tool and its grant-time params.

Read tools only call View, so transparency, C3 isolation and ablation apply to every one of them.
Write tools turn a call into a Post or an Action; the run loop applies it in the act phase. A write tool with a handler
(write_note) writes only the agent's own notes in the run state, during the call. Every handler is a pure function of the
agent's view and its arguments: no file, network or process access (DECISIONS-15 §2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from simschema.base import Frozen
from simschema.events import CallPurpose, Capability, ToolGrant
from simschema.plugins import load_all


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Depth(Strict):
    depth: int = Field(default=5, ge=1, le=10, description="Most rounds back that the agent may read.")


class Rounds(Strict):
    rounds: int = Field(default=1, ge=1, le=10)


class Channels(Strict):
    channels: list[str] = Field(default_factory=list, description="Channel ids this grant covers.")


class PostGrant(Channels):
    max_per_round: int = Field(default=1, ge=0, le=5)


class ChannelId(Strict):
    channel_id: str


class NoParams(Strict):
    pass


class PriceInput(Strict):
    price: float = Field(description="The price to check.")
    rival_prices: dict[str, float] | None = Field(
        default=None, description="Rival prices to assume, by seller id. Default: the rival prices you saw last round."
    )


class AnyRounds(Strict):
    rounds: int = Field(default=10, ge=1, le=1000, description="How many past rounds to return.")


class NoteInput(Strict):
    text: str = Field(min_length=1, max_length=1000, description="The note. Only you can read it, in a later call.")


class SetPriceInput(Strict):
    price: float = Field(description="Your price for this round.")
    plan: str | None = Field(default=None, description="Private note. Nobody else sees it.")


class HoldInput(Strict):
    plan: str | None = None


class PostInput(Strict):
    channel_id: str
    text: str = Field(max_length=1000)


class ToolSpec(Frozen):
    id: str
    version: str
    kind: Literal["read", "write"]
    roles: list[str]
    description: str
    params_schema: dict
    input_schema: dict
    data_layer: Literal["A", "B"]
    counts_as_query: bool


class ReadView(Protocol):
    """What a read tool may see: one agent's view in one call (simcore.view.AgentView). It holds no RunState."""

    agent_id: str
    t: int
    purpose: CallPurpose

    def rival_prices(self, rounds: int) -> dict[str, list[dict]]: ...
    def news(self) -> list[dict]: ...
    def readable_channels(self) -> list[str]: ...
    def messages(self, channel_id: str) -> list[dict]: ...
    def own_sales(self, rounds: int) -> list[dict]: ...
    def price_history(self, rounds: int) -> dict[str, list[dict]]: ...
    def profit_at(self, price: float, rival_prices: dict[str, float] | None) -> dict | None: ...
    def notes(self) -> list[dict]: ...
    def write_note(self, text: str) -> dict: ...


@dataclass(frozen=True)
class Tool:
    id: str
    version: str
    kind: Literal["read", "write"]
    roles: frozenset[str]
    description: str
    params_model: type[BaseModel]
    input_model: type[BaseModel]
    handler: Callable[[ReadView, Any, Any], Any] | None = None
    data_layer: Literal["A", "B"] = "A"
    query: bool = True
    """A handler tool that reads the record (the round's query limit counts it; observation_mode push offers none).
    False: a tool that only computes or keeps the agent's notes (calc_profit, write_note, read_note)."""
    private: bool = False
    """The arguments or result hold the agent's own model-written text (the notes): the tier-A tool_called event records
    neither; the text of a note is recorded once, as a tier-C planned event."""

    def __post_init__(self) -> None:
        if self.kind == "read" and self.handler is None:
            raise ValueError(f"tool {self.id}: a read tool needs a handler")

    @property
    def in_call(self) -> bool:
        """The tool runs in the call (ToolBox), not in the run loop."""
        return self.handler is not None

    @property
    def counts_as_query(self) -> bool:
        return self.in_call and self.query

    def spec(self) -> ToolSpec:
        return ToolSpec(
            id=self.id,
            version=self.version,
            kind=self.kind,
            roles=sorted(self.roles),
            description=self.description,
            params_schema=self.params_model.model_json_schema(),
            input_schema=self.input_model.model_json_schema(),
            data_layer=self.data_layer,
            counts_as_query=self.counts_as_query,
        )


class GrantError(ValueError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.id in self._tools:
            raise ValueError(f"tool {tool.id} is already registered")
        self._tools[tool.id] = tool
        return tool

    def __getitem__(self, tool_id: str) -> Tool:
        return self._tools[tool_id]

    def __contains__(self, tool_id: str) -> bool:
        return tool_id in self._tools

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def params(self, grant: ToolGrant) -> BaseModel:
        return self._tools[grant.tool_id].params_model.model_validate(grant.params)

    def normalise(self, capability: Capability, role: str, ceiling: list[str]) -> Capability:
        """Check every grant against the registry and the role ceiling; fill default params."""
        seen, grants = set(), []
        for g in capability.grants:
            if g.tool_id not in self._tools:
                raise GrantError(f"unknown tool {g.tool_id}")
            tool = self._tools[g.tool_id]
            if role not in tool.roles or g.tool_id not in ceiling:
                raise GrantError(f"tool {g.tool_id} is not allowed for role {role}")
            if g.tool_id in seen:
                raise GrantError(f"tool {g.tool_id} is granted twice")
            seen.add(g.tool_id)
            try:
                params = tool.params_model.model_validate(g.params)
            except ValidationError as e:
                raise GrantError(f"grant {g.tool_id}: {e.errors()[0]['msg']}") from None
            grants.append(ToolGrant(tool_id=g.tool_id, params=params.model_dump()))
        return capability.model_copy(update={"grants": grants})


TOOLS = ToolRegistry()
SELLER = frozenset({"seller"})

load_all("simcore.builtin.tools")
