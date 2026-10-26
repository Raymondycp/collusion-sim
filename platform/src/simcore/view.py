"""View: the single function that decides what an agent can see. Push observations and every read tool go through it."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from simcore.callplan import CallMode, CallSpec, ObservationMode, Setting, call_plan
from simcore.state import Mask, Message, Note, RunState
from simcore.tools import TOOLS, Tool, ToolSpec
from simschema.events import (
    CallPurpose,
    Capability,
    ChannelInfo,
    NewMessage,
    NewsItem,
    Observation,
    Own,
    OwnRound,
    PricePoint,
    RecentMessage,
    RunLimits,
)

MAX_MESSAGES = 20
MAX_NEWS = 5
MAX_NOTES = 50  # read_note returns the last notes the call may read

PULL_KEEPS = ("t", "own")
"""What a pull observation holds (DECISIONS-15 §3.2): the round and the agent's own last result. Every other field is
reset to its empty value, so a field added to Observation later is empty in pull mode too."""


@dataclass(frozen=True)
class Pricing:
    """calc_profit's market: the sellers and a settlement at the scenario's base demand params, without shocks, so the tool
    shows no shock that the agent was not told about. settle snaps each price to the grid, as the market does, and returns
    the prices, quantities and profits by seller."""

    sellers: tuple[str, ...]
    settle: Callable[[dict[str, float]], tuple[dict[str, float], dict[str, float], dict[str, float]]]


@dataclass
class QueryBudget:
    """Shared by all calls of one agent in one round."""

    limit: int
    used: int = 0
    refused: int = 0
    not_offered: list[str] = field(default_factory=list)  # reads asked for while the call offered none and queries were left

    def take(self) -> bool:
        if self.used >= self.limit:
            self.refused += 1
            return False
        self.used += 1
        return True


ToolRecorder = Callable[[str, str | None, dict[str, Any], bool, Any], None]


def result_hash(result: Any) -> str:
    blob = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class NoCall(LookupError):
    """View.call for a call the plan does not have."""


class AgentView:
    """One agent's view in one call: View bound to the agent, round, purpose and state. Read tools get only this."""

    def __init__(self, view: "View", agent_id: str, t: int, purpose: CallPurpose, state: RunState) -> None:
        self.agent_id, self.t, self.purpose = agent_id, t, purpose
        self.__view, self.__state = view, state

    def rival_prices(self, rounds: int) -> dict[str, list[dict]]:
        return self.__view.rival_prices(self.agent_id, self.t, rounds, self.__state)

    def news(self) -> list[dict]:
        return self.__view.news(self.agent_id, self.t, self.__state)

    def readable_channels(self) -> list[str]:
        return self.__view.readable_channels(self.agent_id, self.purpose)

    def postable_channels(self) -> list[str]:
        return self.__view.postable_channels(self.agent_id)

    def messages(self, channel_id: str) -> list[dict]:
        return self.__view.messages(self.agent_id, channel_id, self.t, self.__state)

    def own_sales(self, rounds: int) -> list[dict]:
        return self.__view.own_sales(self.agent_id, self.t, rounds, self.__state)

    def price_history(self, rounds: int) -> dict[str, list[dict]]:
        return self.__view.price_history(self.agent_id, self.t, rounds, self.__state)

    def profit_at(self, price: float, rival_prices: dict[str, float] | None) -> dict:
        return self.__view.profit_at(self.agent_id, self.t, price, rival_prices, self.__state)

    def notes(self) -> list[dict]:
        return self.__view.notes(self.agent_id, self.purpose, self.__state)

    def write_note(self, text: str) -> dict:
        """The one write a tool makes to the run state: the agent's own note (DECISIONS-15 §3.3)."""
        self.__state.notes.append(Note(t=self.t, agent_id=self.agent_id, purpose=self.purpose, text=text))
        return {"saved": True, "t": self.t}


class ToolBox:
    """The read tools one call may use: the agent's grants, filtered by View for this phase."""

    def __init__(
        self,
        tools: dict[str, tuple[Tool, Any]],
        view: AgentView,
        budget: QueryBudget,
        record: ToolRecorder,
        spec: CallSpec | None = None,
    ) -> None:
        self._tools, self._view, self._budget, self._record = tools, view, budget, record
        self.spec = spec
        """The call's plan (simcore.callplan): the reads it offers, the other tools, the writes and the text channel."""

    @property
    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool, _ in self._tools.values()]

    def has(self, tool_id: str) -> bool:
        return tool_id in self._tools

    @property
    def query_limit(self) -> int:
        """The round's query limit. With 0, no read is ever offered, and a read in a reply is an invalid tool call."""
        return self._budget.limit

    @property
    def queries_left(self) -> int:
        """Reads this seller may still make this round (one budget for the talk and act calls)."""
        return max(self._budget.limit - self._budget.used, 0)

    @property
    def readable(self) -> list[str]:
        return list(self.spec.readable)

    @property
    def postable(self) -> list[str]:
        return list(self.spec.postable)

    def refuse(self, tool_id: str, arguments: dict[str, Any]) -> None:
        """A granted read that this call did not offer, recorded as a refused tool_called. It is a budget refusal (and
        counts in limits_hit) only when no query is left; otherwise it goes to not_offered (CR-46 n4)."""
        tool, _ = self._tools[tool_id]
        if self.queries_left:
            self._budget.not_offered.append(tool_id)
        else:
            self._budget.refused += 1
        self._record(tool.id, tool.version, arguments, False, None)

    def call(self, tool_id: str, **arguments: Any) -> Any:
        if tool_id not in self._tools:
            self._record(tool_id, TOOLS[tool_id].version if tool_id in TOOLS else None, arguments, False, None)
            return None
        tool, params = self._tools[tool_id]
        try:
            args = tool.input_model.model_validate(arguments)
        except ValidationError:
            self._record(tool.id, tool.version, arguments, False, None)
            return None
        if tool.counts_as_query and not self._budget.take():
            self._record(tool.id, tool.version, arguments, False, None)
            return None
        result = tool.handler(self._view, params, args)
        self._record(tool.id, tool.version, arguments, result is not None, result)
        return result


class View:
    def __init__(
        self,
        channels: list[ChannelInfo],
        capabilities: dict[str, Capability],
        c3_isolation: bool,
        pricing: Pricing | None = None,
        mode: ObservationMode = "both",
        call_mode: CallMode = "talk_and_act",
        limits: RunLimits = RunLimits(),
        memory_summary: str = "code",
    ) -> None:
        """mode (DECISIONS-15 §3.2): push, the observation holds the visible record and no call offers a read of the record;
        pull, the observation holds only PULL_KEEPS and the agent reads with its read tools; both. What each call sees and
        offers is the call plan's (simcore.callplan)."""
        self.channels = {c.id: c for c in channels}
        self.plan = call_plan(Setting(capabilities, tuple(channels), call_mode, c3_isolation, mode, limits, memory_summary))
        self.capabilities = {s: p.capability for s, p in self.plan.sellers.items()}
        self.c3_isolation = c3_isolation
        self.pricing = pricing
        self.mode = mode

    def call(self, agent: str, purpose: CallPurpose) -> CallSpec:
        """The plan of one call. A purpose the seller does not have (a talk call of a seller with no postable channel) is
        a program error, not a request error, so it stays a 500: the run asks talks() first (CR-84 small point 2)."""
        calls = self.plan.sellers[agent].calls
        if purpose not in calls:
            raise NoCall(f"{agent} makes no {purpose} call in this run")
        return calls[purpose]

    def _params(self, agent: str, tool_id: str):
        g = self.capabilities[agent].grant(tool_id)
        return TOOLS.params(g) if g else None

    def readable_channels(self, agent: str, purpose: CallPurpose) -> list[str]:
        return list(self.call(agent, purpose).readable)

    def talks(self, agent: str) -> bool:
        """The agent has a talk call: talk_and_act and a postable channel (CR-57 n1). Without one it has no talk call, so
        no talk observation either."""
        return self.plan.sellers[agent].talks

    def receivers(self, sender: str, channel_id: str, deliver_t: int, state: RunState) -> list[str]:
        """Who reads a message at its delivery: participants other than the sender with a read grant on the channel, in a
        call they make: the talk call (only with a talk turn), or the act call (not under c3_isolation). So in C3-silent B,
        with no talk turn and an isolated act call, never receives. A clear mask hides it from all; a shuffle mask keeps it
        readable, with the texts in another order."""
        if any(m.channel_id == channel_id and m.mode == "clear" and deliver_t >= m.from_t for m in state.masks):
            return []
        members = self.channels[channel_id].participants

        def reads(a: str) -> bool:
            return any(channel_id in c.readable for c in self.plan.sellers[a].calls.values())

        return [a for a in members if a != sender and a in self.plan.sellers and reads(a)]

    def postable_channels(self, agent: str) -> list[str]:
        return list(self.plan.sellers[agent].postable)

    def observe(self, agent: str, t: int, state: RunState, purpose: CallPurpose) -> Observation:
        record = self.call(agent, purpose).record
        sees_own, sees_news, sees_rivals = "own" in record, "news" in record, "rival_prices" in record
        last = state.last_price(agent)
        out = state.outcomes.get(agent) or []
        masked = ["channels"] if self.c3_isolation and purpose == "act" else []
        if not sees_news:
            masked.append("news")
        new = [
            NewMessage(channel_id=c, sender=m["from"], text=m["text"])
            for c in self.readable_channels(agent, purpose)
            for m in self.messages(agent, c, t, state)
            if m["deliver_t"] == t and m["from"] != agent
        ]
        rivals = {r: h[-1]["price"] for r, h in self.rival_prices(agent, t, 1, state).items() if h} if sees_rivals else {}
        # DECISIONS-15 §1: the whole visible record so far, not the last few items (the read tools keep their caps)
        thread = sorted(
            (
                RecentMessage(channel_id=c, t=m["t"], deliver_t=m["deliver_t"], sender=m["from"], text=m["text"])
                for c in self.readable_channels(agent, purpose)
                for m in self.messages(agent, c, t, state, limit=None)
            ),
            key=lambda m: (m.deliver_t, m.t),
        )
        observation = Observation(
            t=t,
            own=Own(
                last_price=last.price if last and sees_own else None,
                last_quantity=out[-1].quantity if out and sees_own else None,
                last_profit=out[-1].profit if out and sees_own else None,
                last_price_set_by_system=bool(last and last.forced),
            ),
            new_message_count=len(new),
            headlines=[n.text for n in state.news if n.t == t and agent in n.visible] if sees_news else [],
            masked=masked,
            rival_last_prices=rivals,
            new_messages=new,
            news=[NewsItem(**n) for n in self.news(agent, t, state, limit=None)] if sees_news else [],
            recent_messages=thread,
            own_history=[OwnRound(**r) for r in self._own_rounds(agent, t, state)] if sees_own else [],
            mask_noop=self.mask_noop(agent, t, state, purpose),
            rival_history={r: [PricePoint(**p) for p in h] for r, h in self._rival_rounds(agent, t, state).items()} if sees_rivals else {},
        )
        return self._for_mode(observation)

    def _for_mode(self, obs: Observation) -> Observation:
        """The last step of observe. Pull keeps PULL_KEEPS; every other field gets its default, or the empty value of its type
        when it has none; masked says that the channels and the news are not in the observation."""
        if self.mode != "pull":
            return obs
        kept = {}
        for name, f in Observation.model_fields.items():
            if name in PULL_KEEPS:
                kept[name] = getattr(obs, name)
            elif f.is_required():
                kept[name] = type(getattr(obs, name))()
        return Observation(**kept).model_copy(update={"masked": ["channels", "news"]})

    def mask_noop(self, agent: str, t: int, state: RunState, purpose: CallPurpose) -> bool:
        """A shuffle mask is active on a channel this call reads, but the other senders' messages visible there have fewer
        than 2 distinct texts, so it changes nothing the reader sees (CR-60 n1, n4: the manipulation check counts these
        rounds)."""
        for c in self.readable_channels(agent, purpose):
            for mask in state.masks:
                if mask.channel_id == c and mask.mode == "shuffle" and t >= mask.from_t:
                    pool = [m for m in state.messages if m.channel_id == c and m.deliver_t <= t and m.sender != agent]
                    if len({m.text for m in pool}) < 2:
                        return True
        return False

    def messages(self, agent: str, channel_id: str, t: int, state: RunState, limit: int | None = MAX_MESSAGES) -> list[dict]:
        """The channel as the agent sees it at t, after masks: the last `limit` messages, or all with None."""
        visible = [m for m in state.messages if m.channel_id == channel_id and m.deliver_t <= t]
        for mask in state.masks:
            if mask.channel_id == channel_id and t >= mask.from_t:
                visible = _apply_mask(mask, visible, agent)
        rows = [{"t": m.t, "from": m.sender, "text": m.text, "deliver_t": m.deliver_t} for m in visible]
        return rows if limit is None else rows[-limit:]

    def rival_prices(self, agent: str, t: int, rounds: int, state: RunState) -> dict[str, list[dict]]:
        params = self._params(agent, "get_rival_prices")
        if params is None:
            return {}
        k = min(rounds, params.depth)
        return {r: h[-k:] for r, h in self._rival_rounds(agent, t, state).items()}

    def _rival_rounds(self, agent: str, t: int, state: RunState) -> dict[str, list[dict]]:
        """Each rival's price for every round before t, only with the get_rival_prices grant."""
        if self._params(agent, "get_rival_prices") is None:
            return {}
        return {r: [{"t": p.t, "price": p.price} for p in h if p.t < t] for r, h in sorted(state.history.items()) if r != agent}

    def news(self, agent: str, t: int, state: RunState, limit: int | None = MAX_NEWS) -> list[dict]:
        """The news the agent may see up to t: the last `limit` items, or all with None."""
        if self._params(agent, "get_news") is None:
            return []
        seen = [{"t": n.t, "text": n.text} for n in state.news if n.t <= t and agent in n.visible]
        return seen if limit is None else seen[-limit:]

    def own_sales(self, agent: str, t: int, rounds: int, state: RunState) -> list[dict]:
        params = self._params(agent, "get_own_sales")
        if params is None:
            return []
        return self._own_rounds(agent, t, state)[-min(rounds, params.depth) :]

    def _own_rounds(self, agent: str, t: int, state: RunState) -> list[dict]:
        """The agent's price, quantity and profit for every round before t."""
        prices = {p.t: p for p in state.history.get(agent, [])}
        return [
            {"t": o.t, "price": prices[o.t].price, "quantity": o.quantity, "profit": o.profit, "set_by_system": prices[o.t].forced}
            for o in state.outcomes.get(agent, [])
            if o.t < t
        ]

    def price_history(self, agent: str, t: int, rounds: int, state: RunState) -> dict[str, list[dict]]:
        """get_price_history: by seller id, the agent's own prices and, only with the get_rival_prices grant, the rivals'
        prices, for the last `rounds` rounds before t. The get_rival_prices depth does not cap it: the history is this
        tool's own grant."""
        sees_rivals = self._params(agent, "get_rival_prices") is not None
        return {
            s: [{"t": p.t, "price": p.price} for p in h if p.t < t][-rounds:]
            for s, h in sorted(state.history.items())
            if s == agent or sees_rivals
        }

    def profit_at(self, agent: str, t: int, price: float, rival_prices: dict[str, float] | None, state: RunState) -> dict:
        """calc_profit: the agent's quantity and profit at a price, with the rival prices it gives or, by default, the rival
        prices it saw last round (none without the get_rival_prices grant). The base demand, no shocks (Pricing)."""
        if self.pricing is None:
            return {"error": "calc_profit is not available in this run"}
        rivals = [s for s in self.pricing.sellers if s != agent]
        if rival_prices is None:
            rival_prices = {r: h[-1]["price"] for r, h in self.rival_prices(agent, t, 1, state).items() if h}
        if sorted(rival_prices) != rivals:
            return {"error": f"give rival_prices for exactly these sellers: {', '.join(rivals)}"}
        prices, quantities, profits = self.pricing.settle({agent: price, **rival_prices})
        return {
            "price": prices[agent],
            "rival_prices": {r: prices[r] for r in rivals},
            "quantity": round(quantities[agent], 4),
            "profit": round(profits[agent], 4),
            "basis": "base demand without shocks",
        }

    def notes(self, agent: str, purpose: CallPurpose, state: RunState) -> list[dict]:
        """read_note: the agent's own notes. Under c3_isolation an act call reads only notes written in act calls
        (DECISIONS-15 §3.3), so a note cannot carry the channel into the pricing call."""
        isolated = self.c3_isolation and purpose == "act"
        mine = [n for n in state.notes if n.agent_id == agent and (not isolated or n.purpose == "act")]
        return [{"t": n.t, "text": n.text} for n in mine[-MAX_NOTES:]]

    def toolbox(self, agent: str, t: int, state: RunState, purpose: CallPurpose, budget: QueryBudget, record: ToolRecorder) -> ToolBox:
        """The tools that run in the call: the plan's reads and other tools (push mode has no read of the record)."""
        spec = self.call(agent, purpose)
        cap = self.capabilities[agent]
        tools = {t: (TOOLS[t], TOOLS.params(cap.grant(t))) for t in spec.reads + spec.other_tools}
        return ToolBox(tools, AgentView(self, agent, t, purpose, state), budget, record, spec)


def _apply_mask(mask: Mask, visible: list[Message], agent: str) -> list[Message]:
    """clear: the channel looks empty from the fork on. shuffle: the other senders' texts, permuted among their own
    messages so that none keeps its text, and their content loses its timing; the reader's own messages stay as sent
    (CR-60 2, n1). A pool with fewer than 2 distinct texts cannot change: Observation.mask_noop records it."""
    if mask.mode == "clear":
        return []
    others = [i for i, m in enumerate(visible) if m.sender != agent]
    order = _derangement(len(others), random.Random(f"shuffle:{mask.channel_id}:{len(visible)}"))
    out = list(visible)
    for i, k in zip(others, order):
        out[i] = visible[i].model_copy(update={"text": visible[others[k]].text})
    return out


def _derangement(n: int, rng: random.Random) -> list[int]:
    """Sattolo's shuffle: one cycle through all n places, so for n >= 2 no place keeps its own item."""
    order = list(range(n))
    for i in range(n - 1, 0, -1):
        j = rng.randrange(i)
        order[i], order[j] = order[j], order[i]
    return order
