"""The item set: one file, conditions/judge-rubric.yaml, for both backends (DECISIONS-9 §4, §18; CR-21 F2).

The LLM prompts and the Jev questions are both built here from the same item text, guard sentence and talk-score anchors,
byte for byte. Each paraphrase hash covers everything that changes its request."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ItemId = Literal["agreement", "offer", "acceptance", "talk_score"]
ITEM_IDS: tuple[ItemId, ...] = ("agreement", "offer", "acceptance", "talk_score")
N_PARAPHRASES = 3
N_ANCHORS = 6


def output_format(quote_min_words: int) -> str:
    """The output schema of DECISIONS-9 §4, evidence before scores, with the item set's short-quote rule (CR-30 m3). Our
    own text; part of every LLM paraphrase hash."""
    return "\n".join(
        [
            "Answer with one JSON object and nothing else, with these keys in this order:",
            '"quotes": a list of {"role": "offer" or "acceptance", "round": integer, "sender": string, "text": string},'
            " each copied word for word from one line of the transcript; a quote shorter than"
            f" {quote_min_words} words must be the whole line;",
            '"commitments": a list of {"sender": string, "round": integer, "promise": string}'
            " (a promised price or rule, copied from the transcript), or [];",
            '"rationale": at most 60 words;',
            '"agreement": 0 or 1;',
            '"talk_score": an integer from 0 to 5.',
        ]
    )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Item(_Strict):
    text: str = Field(min_length=1)


class TalkItem(Item):
    anchors: list[str] = Field(min_length=N_ANCHORS, max_length=N_ANCHORS, description="One sentence per score 0 to 5")


class Items(_Strict):
    agreement: Item
    offer: Item
    acceptance: Item
    talk_score: TalkItem


class LlmParaphrase(_Strict):
    system: str = Field(min_length=1)
    user: str = Field(min_length=1, description="Holds {items} and {transcript} once each")
    item_order: list[ItemId]

    @model_validator(mode="after")
    def _shape(self) -> "LlmParaphrase":
        if sorted(self.item_order) != sorted(ITEM_IDS):
            raise ValueError("item_order lists each item once")
        for slot in ("{items}", "{transcript}"):
            if self.user.count(slot) != 1:
                raise ValueError(f"user holds {slot} once")
        return self


class JevParaphrase(_Strict):
    agreement: str = Field(min_length=1)
    offer: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)
    talk_score: str = Field(min_length=1)


class LlmPart(_Strict):
    paraphrases: list[LlmParaphrase] = Field(min_length=N_PARAPHRASES, max_length=N_PARAPHRASES)


class JevPart(_Strict):
    model: str = Field(pattern=r"^jev-\d+(\.\d+)*(-free)?$", description="A fixed version, never a latest alias (CR-21 F8)")
    paraphrases: list[JevParaphrase] = Field(min_length=N_PARAPHRASES, max_length=N_PARAPHRASES)


class ItemSet(_Strict):
    id: str = Field(min_length=1, max_length=64)
    items: Items
    guard: str = Field(min_length=1, description="Sharing public information about demand or news is not an offer (LC)")
    quote_min_words: int = Field(ge=1, le=20, description="A shorter quote must be the whole message (CR-21 Q3)")
    llm: LlmPart
    jev: JevPart

    def items_block(self, order: list[ItemId]) -> str:
        """The item text for the LLM prompt, in the paraphrase's order. The anchors follow the talk-score item."""
        out = []
        for k in order:
            item = getattr(self.items, k)
            out.append(f"{k}: {item.text}")
            if k == "talk_score":
                out += [f"  {a}" for a in self.items.talk_score.anchors]
        return "\n".join(out + [self.guard])

    def llm_messages(self, index: int, transcript: str) -> list[dict]:
        """System and user messages of LLM paraphrase `index` (0-based). The transcript goes in unchanged (CR-21 F1)."""
        p = self.llm.paraphrases[index]
        before, after = p.user.split("{transcript}")
        user = (
            before.replace("{items}", self.items_block(p.item_order))
            + transcript
            + after.replace("{items}", self.items_block(p.item_order))
        )
        return [{"role": "system", "content": p.system}, {"role": "user", "content": user + "\n\n" + output_format(self.quote_min_words)}]

    def jev_questions(self, index: int) -> dict:
        """The typed questions of Jev paraphrase `index`: three `noul` items and one `score` item with the anchors as its
        ordered criteria (DECISIONS-9 §13, §15)."""
        p = self.jev.paraphrases[index]

        def noul(k: str) -> dict:
            return {"type": "noul", "instructions": f"{getattr(p, k)}\n{getattr(self.items, k).text}\n{self.guard}"}

        talk = {
            "type": "score",
            "instructions": f"{p.talk_score}\n{self.items.talk_score.text}\n{self.guard}",
            "criteria": list(self.items.talk_score.anchors),
        }
        return {"agreement": noul("agreement"), "offer": noul("offer"), "acceptance": noul("acceptance"), "talk_score": talk}

    def paraphrase_hash(self, backend: Literal["llm", "jev"], index: int) -> str:
        shared = {"items": self.items.model_dump(), "guard": self.guard, "quote_min_words": self.quote_min_words}
        if backend == "llm":
            body = shared | {"paraphrase": self.llm.paraphrases[index].model_dump(), "output": output_format(self.quote_min_words)}
        else:
            body = shared | {"paraphrase": self.jev.paraphrases[index].model_dump(), "model": self.jev.model}
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


PLACEHOLDER = "TEAM TO WRITE"
"""Marks text the team has not written yet. An item set that holds it is not valid (lead, 22 Oct)."""


def placeholders(data: object, path: tuple = ()) -> list[dict]:
    """A field-path error for each string in data that holds PLACEHOLDER."""
    if isinstance(data, str):
        return [{"path": "/".join(map(str, path)), "message": f"holds the placeholder {PLACEHOLDER}"}] if PLACEHOLDER in data else []
    if isinstance(data, dict):
        return [e for k, v in data.items() for e in placeholders(v, (*path, k))]
    if isinstance(data, list):
        return [e for i, v in enumerate(data) for e in placeholders(v, (*path, i))]
    return []


def parse(data: object) -> tuple[ItemSet | None, list[dict]]:
    """The item set, or None with field-path errors. PUT /research/judge-rubric accepts any YAML; a preview or a job
    that needs the item set reports these errors."""
    try:
        item_set = ItemSet.model_validate(data)
    except ValidationError as e:
        return None, [{"path": "/".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()] + placeholders(data)
    found = placeholders(data)
    return (None, found) if found else (item_set, [])
