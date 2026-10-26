"""Run state. The whole state is one serialisable value: a snapshot is its JSON."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

from simschema.base import Frozen


class PricePoint(Frozen):
    t: int
    price: float
    forced: bool = False


class Outcome(Frozen):
    t: int
    quantity: float
    profit: float


class Message(Frozen):
    id: int
    t: int
    channel_id: str
    sender: str
    text: str
    deliver_t: int


class NewsItem(Frozen):
    t: int
    event_index: int
    text: str
    visible: list[str]


class Note(Frozen):
    """A note an agent wrote with write_note, readable only by that agent (DECISIONS-15 §3.3)."""

    t: int
    agent_id: str
    purpose: Literal["talk", "act", "one_call"]
    text: str


class Mask(Frozen):
    channel_id: str
    mode: Literal["clear", "shuffle"]
    from_t: int


class RunState(BaseModel):
    t: int = 0
    history: dict[str, list[PricePoint]] = Field(default_factory=dict)
    outcomes: dict[str, list[Outcome]] = Field(default_factory=dict)
    messages: list[Message] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    masks: list[Mask] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    policy_state: dict[str, dict] = Field(default_factory=dict)

    def last_price(self, seller: str) -> PricePoint | None:
        h = self.history.get(seller)
        return h[-1] if h else None

    def snapshot(self) -> tuple[str, str]:
        blob = self.model_dump_json()
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest(), blob

    @classmethod
    def restore(cls, blob: str) -> "RunState":
        return cls.model_validate_json(blob)
