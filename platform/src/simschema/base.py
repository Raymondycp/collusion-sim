from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

ACRONYMS = {"usd": "USD", "id": "ID", "ids": "IDs", "llm": "LLM", "url": "URL", "mtok": "MTok", "c3": "C3"}


def field_title(name: str, _info=None) -> str:
    """Form label of a field: sentence case, acronyms kept ("max_usd" -> "Max USD")."""
    words = [ACRONYMS.get(w, w) for w in name.split("_")]
    return " ".join([words[0][:1].upper() + words[0][1:], *words[1:]])


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", field_title_generator=field_title)


class NoParams(BaseModel):
    """Params of a plug-in (tool or indicator) that takes none."""

    model_config = ConfigDict(extra="forbid")


class DemandParams(Frozen):
    """Fields every demand model has. A model's params class adds its own fields.

    The cost of seller s is c + c_offset[s], so a shock on /c moves every seller and a shock on /c_offset/B moves only B.
    """

    c: float = Field(ge=0, description="Marginal cost, common to all sellers")
    c_offset: dict[str, float] = Field(default_factory=dict, description="Per-seller cost offset, added to c")

    def cost_of(self, seller: str) -> float:
        return self.c + self.c_offset.get(seller, 0.0)
