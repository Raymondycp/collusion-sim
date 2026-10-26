"""The call plan: for each seller and call purpose, what the call sees and which tools it offers (QUEUE rule b,
DE-callplan-report.md R1-R9). One pure function of the run setting; the config, RunSpec, the condition file, View, the
LLM policy, the run loop and the estimate all read it, so no rule is decided twice."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Literal

from simcore.tools import TOOLS
from simschema.events import CallPurpose, Capability, ChannelInfo, RunLimits

CallMode = Literal["one_call", "talk_and_act"]
ObservationMode = Literal["push", "pull", "both"]
Group = Literal["own", "rival_prices", "news", "messages"]
CHANNEL_GRANTS = ("read_channel", "post")
GATES: dict[Group, str] = {"own": "get_own_sales", "rival_prices": "get_rival_prices", "news": "get_news"}
TALK_READS = frozenset({"read_channel"})
"""The reads a talk call may offer, in pull only: the talk observation holds no messages there (CR-71 1)."""
NO_TALK_UNDER_C3 = "reads no channel: no talk turn under isolation"
CANNOT_SEE = "has no queries and cannot see"


@dataclass(frozen=True)
class Setting:
    """The run setting the plan reads. `channels`: the open channels. news and transparency only trim grants (R1); a
    capability that is already effective (a run header) gives the same plan with their defaults."""

    capabilities: dict[str, Capability]
    channels: tuple[ChannelInfo, ...]
    call_mode: CallMode = "talk_and_act"
    c3_isolation: bool = False
    observation_mode: ObservationMode = "both"
    limits: RunLimits = RunLimits()
    memory_summary: Literal["code", "llm", "none"] = "code"
    news: bool = True
    transparency: str | None = None


@dataclass(frozen=True)
class CallSpec:
    record: frozenset[Group]
    summary: bool
    readable: tuple[str, ...]
    postable: tuple[str, ...]
    text_channel: str | None
    reads: tuple[str, ...]
    """Query tools the call can run: a read asked for outside `offered` is a recorded refusal (DECISIONS-12 §2a)."""
    offered: tuple[str, ...]
    """The reads offered in the request while a query is left (talk and act share SellerPlan.query_limit)."""
    other_tools: tuple[str, ...]
    writes: tuple[str, ...]
    notes_from: frozenset[CallPurpose]


@dataclass(frozen=True)
class SellerPlan:
    capability: Capability
    query_limit: int
    calls: dict[CallPurpose, CallSpec]
    notes: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()

    @property
    def talks(self) -> bool:
        return "talk" in self.calls

    @property
    def postable(self) -> tuple[str, ...]:
        return next((c.postable for c in self.calls.values() if c.postable), ())

    def steps(self, purpose: CallPurpose, max_steps: int) -> int:
        """Tool steps of one turn at worst. With no query budget a turn has one step (DECISIONS-13 §1): the act call offers
        only set_price and hold, and a talk call with one channel posts its text reply. A talk call with more than one
        channel uses the post tool, and a refused post takes another step."""
        if self.query_limit > 0 or len(self.calls[purpose].postable) > 1:
            return max_steps
        return 1


@dataclass(frozen=True)
class CallPlan:
    sellers: dict[str, SellerPlan]
    refused: tuple[str, ...] = field(default=())
    """Run-level refusals and every seller's, in order: the first one is the 422 message."""


def possible_channels(sellers: list[str]) -> dict[str, frozenset[str]]:
    """Each channel id a run of these sellers can have, with its participants (CR-79 M1)."""
    ids = sorted(sellers)
    pairs = {f"private_{a}_{b}": frozenset((a, b)) for a, b in itertools.combinations(ids, 2)}
    return {"public": frozenset(ids), **pairs}


def open_channels(sellers: list[str], public: bool, private_pairs: bool) -> tuple[ChannelInfo, ...]:
    ids = sorted(sellers)
    out = [ChannelInfo(id="public", kind="public", participants=ids)] if public else []
    if private_pairs:
        out += [ChannelInfo(id=f"private_{a}_{b}", kind="private", participants=[a, b]) for a, b in itertools.combinations(ids, 2)]
    return tuple(out)


def push_gaps(capability: Capability, news: bool, channels: bool) -> list[str]:
    """What a seller with no query budget cannot see in its observation (DECISIONS-13 §1): its own price, sales and profit
    (get_own_sales), the news when the condition publishes news (get_news), and the messages of its channels (read_channel).
    Rival prices follow transparency (the condition file checks that grant)."""
    need = {"own price, sales and profit": "get_own_sales"}
    if news:
        need["news"] = "get_news"
    if channels and capability.grant("post") is not None:
        need["channel messages"] = "read_channel"
    return [what for what, tool in need.items() if capability.grant(tool) is None]


def text_channel(purpose: CallPurpose, postable: list[str] | tuple[str, ...]) -> str | None:
    """The channel a plain-text talk reply is posted to (exactly one postable channel), or None when the talk turn uses
    the post tool."""
    return postable[0] if purpose == "talk" and len(postable) == 1 else None


def call_plan(setting: Setting) -> CallPlan:
    run_refused = []
    if setting.c3_isolation and setting.call_mode != "talk_and_act":
        run_refused.append("c3_isolation needs call_mode talk_and_act")
    sellers = {s: _seller(s, cap, setting) for s, cap in setting.capabilities.items()}
    return CallPlan(sellers, tuple(run_refused) + tuple(r for p in sellers.values() for r in p.refused))


def _effective(seller: str, capability: Capability, setting: Setting) -> tuple[Capability, list[str]]:
    """R1 and R9 (c): switches win; a possible but closed channel is trimmed; an impossible channel is refused."""
    possible = possible_channels(list(setting.capabilities))
    opened = {c.id: c for c in setting.channels}
    off = ({"get_news"} if not setting.news else set()) | ({"get_rival_prices"} if setting.transparency == "opaque" else set())
    grants, refused = [], []
    for g in capability.grants:
        if g.tool_id in off:
            continue
        if g.tool_id in CHANNEL_GRANTS:
            named = g.params.get("channels") or []
            if bad := [c for c in named if seller not in possible.get(c, ())]:
                refused.append(f"seller {seller}: {g.tool_id} names {bad}, which is not public or a private pair of {seller}")
            kept = [c for c in named if c in opened and seller in opened[c].participants]
            if not kept:
                continue
            if kept != named:
                g = g.model_copy(update={"params": {**g.params, "channels": kept}})
        grants.append(g)
    if capability.grant("set_price") is None:
        refused.append(f"seller {seller} has no set_price grant")
    effective = capability if grants == capability.grants else capability.model_copy(update={"grants": grants})
    return effective, refused


def _seller(seller: str, capability: Capability, setting: Setting) -> SellerPlan:
    cap, refused = _effective(seller, capability, setting)
    granted = {g.tool_id for g in cap.grants}
    limit = setting.limits.queries(cap.max_queries_per_round)[0]
    mode = setting.observation_mode
    if mode == "pull" and limit == 0:
        refused.append(f"observation_mode pull needs a query limit above 0; seller {seller} has none")

    def channels(tool_id: str) -> tuple[str, ...]:
        g = cap.grant(tool_id)
        return tuple(g.params.get("channels") or []) if g else ()

    postable = channels("post")
    readable = channels("read_channel")
    purposes: tuple[CallPurpose, ...] = ("one_call",) if setting.call_mode == "one_call" else (("talk",) if postable else ()) + ("act",)
    order = [g.tool_id for g in cap.grants if g.tool_id in TOOLS]  # the grant order is the offer order (request hash)
    queries = [t for t in order if TOOLS[t].counts_as_query]
    others = tuple(t for t in order if TOOLS[t].in_call and not TOOLS[t].query)

    calls = {}
    for p in purposes:
        isolated = setting.c3_isolation and p == "act"
        sees = () if isolated else readable
        if mode == "pull":
            record = {"own"} if "get_own_sales" in granted else set()
        else:
            record = {g for g, tool in GATES.items() if tool in granted} | ({"messages"} if sees else set())
        reads = tuple(t for t in queries if t != "read_channel" or sees) if mode != "push" and limit > 0 else ()
        offered = tuple(t for t in reads if p != "talk" or (mode == "pull" and t in TALK_READS))
        text = text_channel(p, postable)
        writes = {"talk": ("post",), "act": ("set_price", "hold"), "one_call": ("post", "set_price", "hold")}[p]
        writes = tuple(w for w in writes if w in granted and (w != "post" or (postable and text is None)))
        calls[p] = CallSpec(
            record=frozenset(record),
            summary=setting.memory_summary != "none",
            readable=sees,
            postable=postable if p != "act" else (),
            text_channel=text,
            reads=reads,
            offered=offered,
            other_tools=others,
            writes=writes,
            notes_from=frozenset({"act"}) if isolated else frozenset({"talk", "act", "one_call"}),
        )
    notes = (NO_TALK_UNDER_C3,) if setting.c3_isolation and readable and "talk" not in calls else ()
    if (mode == "push" or limit == 0) and (gaps := push_gaps(cap, setting.news, bool(setting.channels))):
        notes += (f"seller {seller} {CANNOT_SEE}: {', '.join(gaps)}",)
    return SellerPlan(cap, limit, calls, notes, tuple(refused))
