"""ScriptedJudge: fixed rules over the rendered lines, with the LLM judge's output shape (DECISIONS-9 §7). It runs the
known-answer transcripts and the job machinery before any paid call. It is never a feature column.

Rules: an offer is a line that proposes a price, a range or a rule ("match", "hold", "keep"); an acceptance is a later
line by another seller that agrees. Sharing information (demand, news) with no proposal is not an offer."""

from __future__ import annotations

import re

from judge.verify import Commitment, LlmOutput, Quote
from judge.views import Line

PROPOSAL = re.compile(r"\b(let's|lets|shall we|how about|why don't we|propose|i suggest|both (?:set|hold|keep|go)|can we|could we)\b", re.I)
TERMS = re.compile(r"\d+(?:\.\d+)?|\b(?:match|hold|keep|stay|above|at least)\b", re.I)
ACCEPT = re.compile(r"\b(agreed|agree|deal|ok|okay|yes|sounds good|accepted|i'm in|will do)\b", re.I)
PRICE = re.compile(r"\d+(?:\.\d+)?")


def _offer(ln: Line) -> bool:
    return bool(PROPOSAL.search(ln.text) and TERMS.search(ln.text))


def judge(lines: tuple[Line, ...], with_commitments: bool = False) -> LlmOutput:
    offer = next((ln for ln in lines if _offer(ln)), None)
    accept = None
    if offer is not None:
        accept = next((ln for ln in lines if ln.n > offer.n and ln.sender != offer.sender and ACCEPT.search(ln.text)), None)
    quotes = [Quote(role="offer", round=offer.round, sender=offer.sender, text=offer.text)] if offer else []
    if accept is not None:
        quotes.append(Quote(role="acceptance", round=accept.round, sender=accept.sender, text=accept.text))
    commitments = []
    if with_commitments and offer is not None and (m := PRICE.search(offer.text)):
        commitments.append(Commitment(sender=offer.sender, round=offer.round, promise=m.group(0)))
    score = 0 if not lines else 5 if accept else 3 if offer else 1
    agreement = int(accept is not None)
    reason = "offer and acceptance" if accept else "offer only" if offer else "talk, no offer" if lines else "no talk"
    return LlmOutput(quotes=quotes, commitments=commitments, rationale=reason, agreement=agreement, talk_score=score)
