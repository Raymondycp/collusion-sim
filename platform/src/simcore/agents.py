"""The AgentPolicy port. Scripted policies and LLM policies both implement it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from pydantic import model_validator

from simcore.market import Market
from simcore.view import ToolBox
from simschema.base import DemandParams, Frozen
from simschema.events import FallbackReason, Observation


class CapabilityError(ValueError):
    """A policy tried to write something that its capability does not allow."""


class BudgetExceeded(RuntimeError):
    """Raised by a policy before a model call that the run budget does not allow. The run stops cleanly."""

    end_reason = "budget_exceeded"


class RateWindowFull(BudgetExceeded):
    """The provider profile's 5-hour call window is full. The run stops like a budget stop, with its own end reason."""

    end_reason = "rate_window"


class Post(Frozen):
    channel_id: str
    text: str


class Action(Frozen):
    price: float | None = None
    hold: bool = False
    plan: str | None = None
    fallback: bool = False
    fallback_reason: FallbackReason | None = None

    @model_validator(mode="after")
    def _one(self) -> "Action":
        if (self.price is None) != self.hold:
            raise ValueError("an action sets a price or holds, not both and not neither")
        return self


@dataclass(frozen=True)
class PolicyContext:
    """Facts a scripted benchmark agent may use. It still sees rivals only through its ToolBox."""

    seller_id: str
    sellers: tuple[str, ...]
    market: Market
    params: Callable[[int], DemandParams]


class AgentPolicy(Protocol):
    def talk(self, obs: Observation, tools: ToolBox) -> list[Post]: ...

    def act(self, obs: Observation, tools: ToolBox) -> Action: ...

    def turn(self, obs: Observation, tools: ToolBox) -> tuple[list[Post], Action]: ...

    def after_round(self, t: int) -> tuple[str, int] | None:
        """Return (memory summary, first round it covers) to record, or None."""

    def dump(self) -> dict: ...

    def load(self, state: dict) -> None: ...
