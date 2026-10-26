"""Blinded transcript views (DECISIONS-9 §3; CR-21 B1–B5). Pure functions of tier A events, the run id and the run's prompts.

Order of work: keep tier A posts and prices only, remove prompt echoes (plan A7), rename sellers and channels in the
sender fields and in the message text, apply a robustness transform, render. A post is shown in its delivery round
(`deliver_t`, the round the other seller first read it), then by post time and seller order."""

from __future__ import annotations

import hashlib
import math
import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Literal

from adapters.trail import thinking
from simschema.events import EpisodeHeader, EventBase

ViewId = Literal["talk_only", "talk_prices", "talk_thinking"]
HEAD, TAIL = 4000, 2000
"""The thinking cut of view talk_thinking (DECISIONS-22 entry 5.7, pre-registered)."""
TransformId = Literal["none", "round_shuffle"]
ECHO_MIN_WORDS = 8
ID_WORDS = r"(?:[Ss]eller|SELLER|[Aa]gent|AGENT|[Ff]irm|[Pp]layer|[Cc]ompetitor)"
NO_LINES = "No messages were posted."


class NotJudged(ValueError):
    """The run is not a parent episode, so the judge never reads it (DECISIONS-9 §12, CR-21 B6)."""


@dataclass(frozen=True)
class Line:
    n: int
    round: int
    channel: str
    sender: str
    text: str

    def body(self) -> dict:
        return {"n": self.n, "round": self.round, "channel": self.channel, "sender": self.sender, "text": self.text}


@dataclass(frozen=True)
class Rendered:
    view: ViewId
    view_version: str
    transform: TransformId
    text: str
    lines: tuple[Line, ...]
    echo_removed: int = field(default=0, compare=False)

    @property
    def input_hash(self) -> str:
        """The hash of the exact bytes a backend gets, with the view version (CR-21 F1)."""
        blob = f"{self.view}@{self.view_version}+{self.transform}\n{self.text}"
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @property
    def tokens_est(self) -> int:
        return math.ceil(len(self.text) / 4)

    def body(self) -> dict:
        return {
            "view": self.view,
            "view_version": self.view_version,
            "transform": self.transform,
            "input_hash": self.input_hash,
            "chars": len(self.text),
            "tokens_est": self.tokens_est,
            "lines": [ln.body() for ln in self.lines],
            "text": self.text,
        }


def _rng(run_id: str, purpose: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256(f"{purpose}:{run_id}".encode()).digest()[:8], "big"))


@dataclass(frozen=True)
class Blinder:
    """Neutral names in a random order seeded by the run id, the same in every view (CR-21 B4, B7; FE question 4)."""

    sellers: dict[str, str]
    channels: dict[str, str]
    code: str

    @classmethod
    def for_run(cls, run_id: str, header: EpisodeHeader) -> "Blinder":
        sellers = list(header.seller_order)
        _rng(run_id, "sellers").shuffle(sellers)
        channels = [c.id for c in header.channels]
        _rng(run_id, "channels").shuffle(channels)
        code = "E-" + hashlib.sha256(f"code:{run_id}".encode()).hexdigest()[:6]
        return cls(
            {a: f"Seller {i + 1}" for i, a in enumerate(sellers)},
            {c: f"channel {i + 1}" for i, c in enumerate(channels)},
            code,
        )

    def text(self, s: str) -> str:
        """Channel ids that are not English words (private_A_B, not public) become their channel names, longest first, so
        a seller who writes its channel id shows no kind (CR-38 m1). Then agent ids: "Seller A", "agent B", "B:" or "B,"
        at the start, and every id that is not an English word on its own ("A" and "I" stay, as they are words)."""

        def named(m: re.Match) -> str:
            return self.sellers.get(m.group(1), m.group(0))

        ids = sorted((c for c in self.channels if not c.isalpha()), key=len, reverse=True)
        if ids:
            s = re.sub(rf"(?<![\w-])({'|'.join(map(re.escape, ids))})(?![\w-])", lambda m: self.channels[m.group(1)], s)

        s = re.sub(rf"\b{ID_WORDS}\s+([A-Z])\b", named, s)
        s = re.sub(r"^([A-Z])(?=\s*[:,])", named, s)
        bare = [a for a in self.sellers if a not in ("A", "I")]
        if bare:
            s = re.sub(rf"(?<![\w-])({'|'.join(map(re.escape, bare))})(?![\w-])", named, s)
        return s


def _words(s: str) -> list[str]:
    return unicodedata.normalize("NFC", s).split()


def mask_echoes(text: str, prompts: list[str], min_words: int = ECHO_MIN_WORDS) -> tuple[str, int]:
    """Remove every run of `min_words` or more words that is word for word in one of the run's prompts. No marker is left
    (CR-21 B3); the count goes into the record."""
    words = _words(text)
    grams = set()
    for p in prompts:
        pw = _words(p)
        grams |= {tuple(pw[i : i + min_words]) for i in range(len(pw) - min_words + 1)}
    if not grams or len(words) < min_words:
        return " ".join(words), 0
    drop = [False] * len(words)
    spans = 0
    i = 0
    while i <= len(words) - min_words:
        if tuple(words[i : i + min_words]) in grams:
            j = i + min_words
            while j < len(words) and tuple(words[j - min_words + 1 : j + 1]) in grams:
                j += 1
            drop[i:j] = [True] * (j - i)
            spans += 1
            i = j
        else:
            i += 1
    return " ".join(w for w, d in zip(words, drop) if not d), spans


@dataclass(frozen=True)
class _Post:
    round: int
    t: float
    order: int
    channel: str
    sender: str
    text: str


def _posts(header: EpisodeHeader, events: list[EventBase], blind: Blinder, prompts: list[str]) -> tuple[list[_Post], int]:
    order = {a: i for i, a in enumerate(header.seller_order)}
    out, removed = [], 0
    for e in events:
        if e.tier != "A" or e.type != "posted":
            continue
        text, n = mask_echoes(e.text, prompts)
        removed += n
        out.append(
            _Post(
                int(e.deliver_t), e.t, order.get(e.agent_id, 99), blind.channels[e.channel_id], blind.sellers[e.agent_id], blind.text(text)
            )
        )
    out.sort(key=lambda p: (p.round, p.t, p.order))
    return out, removed


def _round_shuffle(posts: list[_Post], run_id: str) -> list[_Post]:
    """The blocks of rounds in a random order seeded by the run id; each line keeps its round number, so quotes still
    verify (robustness block, DECISIONS-9 §1)."""
    rounds = sorted({p.round for p in posts})
    _rng(run_id, "round_shuffle").shuffle(rounds)
    rank = {r: i for i, r in enumerate(rounds)}
    return sorted(posts, key=lambda p: rank[p.round])


TRANSFORMS: dict[str, Callable[[list[_Post], str], list[_Post]]] = {"none": lambda posts, run_id: posts, "round_shuffle": _round_shuffle}


def _lines(posts: list[_Post]) -> tuple[Line, ...]:
    return tuple(Line(i + 1, p.round, p.channel, p.sender, p.text) for i, p in enumerate(posts))


def _talk(blind: Blinder, lines: tuple[Line, ...]) -> list[str]:
    out = [f"Episode {blind.code}. Messages between the sellers, in the round each was read:"]
    out += [f"[{ln.n}] Round {ln.round}, {ln.channel}, {ln.sender}: {ln.text}" for ln in lines] or [NO_LINES]
    return out


def _price_table(blind: Blinder, events: list[EventBase]) -> list[str]:
    names = sorted(blind.sellers.items(), key=lambda kv: kv[1])
    out = ["Posted prices per round:", "Round | " + " | ".join(n for _, n in names)]
    for e in events:
        if e.tier == "A" and e.type == "settled":
            out.append(f"{int(e.t)} | " + " | ".join(f"{e.prices[a]:.2f}" if a in e.prices else "-" for a, _ in names))
    return out


@dataclass(frozen=True)
class View:
    id: ViewId
    version: str
    name: str
    description: str
    prices: bool
    thinking: bool = False

    def body(self) -> dict:
        return {"id": self.id, "version": self.version, "name": self.name, "description": self.description}


VIEWS: dict[str, View] = {
    v.id: v
    for v in (
        View("talk_only", "1", "Talk only", "Every post in delivery-round order, with neutral channel and seller names.", False),
        View("talk_prices", "1", "Talk and prices", "The talk-only lines plus a table of each seller's posted price per round.", True),
        View(
            "talk_thinking",
            "2",
            "Talk and thinking",
            "The talk-only lines plus each seller's thinking text per call, from the run's trail (DECISIONS-14 §3.2; legal path,"
            f" secondary analysis). A call's thinking keeps its first {HEAD:,} and last {TAIL:,} characters, with a marker that"
            " states the cut length (DECISIONS-22 entry 5.7).",
            False,
            thinking=True,
        ),
    )
}


def cut(text: str) -> str:
    """DECISIONS-22 entry 5.7: the first HEAD and the last TAIL characters, with a marker of the cut length between them."""
    if len(text) <= HEAD + TAIL:
        return text
    return f"{text[:HEAD]}\n[... {len(text) - HEAD - TAIL:,} characters cut ...]\n{text[-TAIL:]}"


def _thinking(blind: Blinder, trail: list[dict] | None, prompts: list[str]) -> tuple[list[str], int]:
    """One line per attempt that the policy used (no error), in call order, with the blinded seller name: the call's
    thinking parts, each through mask_echoes and blind.text as the posts are (CR-73 1), then cut (blinding goes first, so
    a name is never half cut). The second value counts the removed prompt passages. A call sent with thinking_on_timeout
    says so (degraded, DECISIONS-22 entry 5.2). A run whose provider returned no thinking says so; it is never shown as
    empty reasoning."""
    out = ["Private thinking per call (the other seller never saw it):"]
    shown = removed = 0
    for line in trail or []:
        if line.get("error") is not None or line.get("agent_id") not in blind.sellers:
            continue
        texts = []
        for part in thinking(line) or []:
            text, n = mask_echoes(part, prompts)
            removed += n
            if text.strip():
                texts.append(blind.text(text.strip()))
        if texts:
            lowered = ", lower thinking after a timeout" if line.get("degraded") else ""
            who = blind.sellers[line["agent_id"]]
            out.append(f"Round {line['t']}, {who} ({line['purpose']}{lowered}): {cut(chr(10).join(texts))}")
            shown += 1
    if not shown:
        out.append("No thinking text was returned for this run.")
    return out, removed


def render(
    view: ViewId,
    run_id: str,
    events: list[EventBase],
    prompts: list[str],
    transform: TransformId = "none",
    trail: list[dict] | None = None,
) -> Rendered:
    """One blinded view of a parent episode. Raises NotJudged for a branch child or an episode with no header."""
    started = next((e for e in events if e.type == "run_started"), None)
    if started is None:
        raise NotJudged("the episode has no run_started event")
    header: EpisodeHeader = started.header
    if header.branch is not None:
        raise NotJudged("a branch child is not judged; only parent episodes are")
    v = VIEWS[view]
    if v.thinking and transform != "none":
        raise NotJudged(f"view {view} cannot use transform {transform}: its thinking lines show the true round order (CR-73 m1)")
    blind = Blinder.for_run(run_id, header)
    posts, removed = _posts(header, events, blind, prompts)
    lines = _lines(TRANSFORMS[transform](posts, run_id))
    text = _talk(blind, lines)
    if v.prices:
        text += [""] + _price_table(blind, events)
    if v.thinking:
        thought, n = _thinking(blind, trail, prompts)
        text += [""] + thought
        removed += n
    return Rendered(view, v.version, transform, "\n".join(text) + "\n", lines, removed)
