"""Judge outputs and the quote check (DECISIONS-9 §4; CR-21 Q1–Q6).

A quote must be an exact substring of the one message at its (round, sender), after one normalisation applied to both
sides (whitespace, quote marks, markdown `*`). A quote shorter than the item set's minimum must be the whole message. A missing offer or acceptance quote forces
agreement to 0 (recorded). A quote or commitment that is not found is dropped and counted (Raymond, 24 Oct: a judge that
invents one quote keeps its other evidence); agreement still needs a found offer and a found acceptance."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from judge.views import Line

QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " "})
MAX_RATIONALE_WORDS = 60


def normalise(s: str) -> str:
    """Whitespace, typographic quotes and markdown emphasis (*) are formatting, not content."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s).translate(QUOTES).replace("*", "")).strip()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Quote(_Strict):
    role: Literal["offer", "acceptance"]
    round: int
    sender: str
    text: str = Field(min_length=1)


class Commitment(_Strict):
    sender: str
    round: int
    promise: str = Field(min_length=1)


class LlmOutput(_Strict):
    quotes: list[Quote]
    commitments: list[Commitment]
    rationale: str
    agreement: Literal[0, 1]
    talk_score: int = Field(ge=0, le=5)

    @field_validator("rationale")
    @classmethod
    def _short(cls, v: str) -> str:
        if len(v.split()) > MAX_RATIONALE_WORDS:
            raise ValueError(f"at most {MAX_RATIONALE_WORDS} words")
        return v


@dataclass(frozen=True)
class Checked:
    """The verified output: the found quotes with their line numbers, the agreement after the missing-quote rule, and how
    many quotes and commitments were dropped as not found."""

    status: Literal["ok", "invalid"]
    reason: str | None
    quote_lines: tuple[int, ...] = ()
    quotes: tuple[Quote, ...] = ()
    commitments: tuple[Commitment, ...] = ()
    dropped: int = 0
    agreement: int | None = None
    agreement_forced: bool = False
    talk_score: int | None = None


def _find(lines: tuple[Line, ...], rnd: int, sender: str, text: str, min_words: int | None) -> Line | None:
    want = normalise(text)
    if not want:  # CR-30 m1: "" is in every line
        return None
    for ln in lines:
        if ln.round != rnd or ln.sender != sender:
            continue
        have = normalise(ln.text)
        if want not in have:
            continue
        if min_words is not None and len(want.split()) < min_words and want != have:
            continue
        return ln
    return None


def _offer_then_acceptance(quotes: list[Quote]) -> bool:
    """An acceptance by another seller, in the offer's round or later (CR-30 m2). Rounds, not line numbers, so the rule
    also holds after the round shuffle."""
    offers = [q for q in quotes if q.role == "offer"]
    return any(a.sender != o.sender and a.round >= o.round for o in offers for a in quotes if a.role == "acceptance")


def check(out: LlmOutput, lines: tuple[Line, ...], quote_min_words: int, commitments_allowed: bool) -> Checked:
    """Verify against the lines that were rendered and sent (CR-21 Q4), never the raw episode."""
    found, lines_found = [], []
    for q in out.quotes:
        if (ln := _find(lines, q.round, q.sender, q.text, quote_min_words)) is not None:
            found.append(q)
            lines_found.append(ln.n)
    if out.commitments and not commitments_allowed:
        return Checked("invalid", "commitments are only for the talk_prices view")
    promises = [c for c in out.commitments if _find(lines, c.round, c.sender, c.promise, None) is not None]
    dropped = len(out.quotes) - len(found) + len(out.commitments) - len(promises)
    forced = out.agreement == 1 and not _offer_then_acceptance(found)
    reason = f"{dropped} quotes or commitments not found, dropped" if dropped else None
    return Checked(
        "ok",
        reason,
        quote_lines=tuple(lines_found),
        quotes=tuple(found),
        commitments=tuple(promises),
        dropped=dropped,
        agreement=0 if forced else out.agreement,
        agreement_forced=forced,
        talk_score=out.talk_score,
    )
