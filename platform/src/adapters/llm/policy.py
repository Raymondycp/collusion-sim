"""LLMPolicy: an AgentPolicy that asks a model, through our own tool loop.

Per call: our system prompt, one user message rendered from the Observation (plus memory notes), and exactly the
tools that the capability grants and View allows. A reply that names an unknown tool or has bad arguments is resent
unchanged (only the seed follows the attempt). A text-only reply where a tool call is required is resent with NUDGE
added. A slow call (SLOW: the deadline or a stalled stream) sends the next attempt with the model's
thinking_on_timeout; a second slow call ends the call. Other failures, a connect or pool timeout among them, are resent
unchanged. At most ATTEMPTS attempts in all, whatever the cause (DECISIONS-22 entry 5.4). Then the seller holds with
fallback=true, or a talk call records PASS, each with its reason. A slow call is recorded in the cache, so a rerun
replays it (entry 5.5).
A post to a channel the seller cannot use is refused as a tool result, so the model can try again; it never ends
the run. Read tools are offered only while the
round's query budget has room (PILOT-1c), so the model is never offered a read that would be refused. A talk call
offers no read tool: the observation already holds the rivals' last prices and the new messages (PILOT-1c, lead).
With a query limit of 0 (all research conditions, DECISIONS-13 §1) a read in a reply is an invalid tool call, so each
call ends in one step: one talk call and one act call per round when every reply is valid.

Talk turn of a seller with one postable channel (PILOT-1c): no post tool. The plain-text reply is posted to that channel
unchanged (surrounding white space removed); PASS posts nothing. Each LLM talk turn ends with one talk_replied event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import httpx2
from pydantic import ValidationError

from adapters.llm.budget import BudgetGuard, RateWindow
from adapters.llm.cache import CallCache
from adapters.llm.endpoint import Endpoint, EndpointError, Reply, Request
from simcore.agents import Action, BudgetExceeded, Post, RateWindowFull
from simcore.callplan import text_channel
from simcore.recorder import Recorder
from simcore.tools import TOOLS
from simcore.view import ToolBox
from simschema.events import (
    BudgetWarningEvent,
    CallPurpose,
    Capability,
    LimitName,
    LlmCallEvent,
    Observation,
    TalkRepliedEvent,
    TalkTextEvent,
)


@dataclass
class _Fetched:
    reply: Reply | None = None
    cache_hit: bool = False
    charged: bool = False
    error: str | None = None
    http_status: int | None = None
    provider_code: str | None = None
    provider_message: str | None = None
    fatal: EndpointError | None = None
    cost_usd: float | None = 0.0
    latency_ms: int = 0
    slot_wait_ms: int | None = None
    first_byte_ms: int | None = None


ATTEMPTS = 3
SLOW = frozenset({"call_timeout", "stream_stalled"})
"""Provider codes of a slow model: the call deadline, or no stream data for STALL_S (DECISIONS-22 entry 5.4; CR-88
addendum 3 points 2 and 4, lead 23 Oct). They take the lowered retry, then the fallback, and are recorded. A connect,
write or pool timeout and stream_truncated are resent unchanged and never recorded."""
OPEN_OUTPUT_TOKENS = 32_000
"""Output tokens that the budget holds and the estimate counts for a call with no output limit (DECISIONS-18 entry 2)."""
NUDGE = "Reply with a tool call, not text."
PASS = "PASS"
NO_LOOKUP = {"refused": True, "reason": "no look-up is available in this call"}
SUMMARY_INSTRUCTION = "Write a short note for yourself (at most 60 words) about these rounds. Reply with plain text."

LOOKUP = "Use the tools to look things up if you want. Then "
INSTRUCTION = {
    "act": "Call set_price, or hold to keep your last price.",
    "talk": "You may post one message with post. To post nothing, reply with plain text.",
    "one_call": "You may post one message with post. Then call set_price, or hold to keep your last price.",
}


MARKET_FACTS = (
    "Market facts: your unit cost is {cost} per unit. Prices of {min} or more, in steps of {step}, are accepted."
    " Your sales fall as your price rises and rise as the rival's price rises."
)
"""The passage every seller call states, in every condition (DECISIONS-22 entry 5.1): the seller's base unit cost at round
1 and the grid's minimum and step, never its maximum (DECISIONS-30 §3.1: a stated maximum is a focal point; the grid max
is a technical bound only), then the direction of demand, the same for every group (DECISIONS-25 entry 1): not the demand
formula, so sellers still learn demand from feedback. No horizon (DECISIONS-13 §6.2) and no per-round cost: cost shocks
reach the sellers only through news."""


OPENING_PRICE = " When trading opens, the market price is {price}."
"""Added to the facts when the run config sets opening_price: a reference, not a cap (Raymond via Grok, 24 Oct)."""


def market_facts(unit_cost: float, grid, opening_price: float | None = None) -> str:
    """`grid`: the market's PriceGrid."""
    facts = MARKET_FACTS.format(cost=_num(unit_cost), min=_num(grid.min), step=_num(grid.step))
    return facts if opening_price is None else facts + OPENING_PRICE.format(price=_num(opening_price))


def request_seed(run_seed: int, seller: str, t: int, sample_index: int, attempt: int) -> int:
    return int(hashlib.sha256(f"{run_seed}:{seller}:{t}:{sample_index}:{attempt}".encode()).hexdigest(), 16) % 2**31


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _num(x: float | None) -> str:
    return "unknown" if x is None else f"{x:g}"


def render(obs: Observation, notes: list[str], purpose: CallPurpose, postable: list[str] = (), lookups: bool = False, me: str = "") -> str:
    """lookups: the call offers read tools (only with a query budget, never in research conditions). me: the seller id, so
    its own messages in the thread read "you"."""
    own = obs.own
    lines = [f"Round {obs.t}."]
    for r in (obs.own_history or [])[:-1]:  # the last round has its own line below
        forced = " (set by the system)" if r.set_by_system else ""
        lines.append(f"Round {r.t}: your price {_num(r.price)}{forced}, units sold {_num(r.quantity)}, profit {_num(r.profit)}.")
    if own.last_price is not None:
        forced = " (set by the system)" if own.last_price_set_by_system else ""
        lines.append(
            f"Last round: your price {_num(own.last_price)}{forced}, units sold {_num(own.last_quantity)}, profit {_num(own.last_profit)}."
        )
    for r, h in (obs.rival_history or {}).items():  # the last round is in the line below
        if len(h) > 1:
            lines.append(f"Earlier prices of {r}: " + ", ".join(f"round {p.t} {_num(p.price)}" for p in h[:-1]) + ".")
    if obs.rival_last_prices:
        lines.append("Rival prices last round: " + ", ".join(f"{r} {_num(p)}" for r, p in obs.rival_last_prices.items()) + ".")
    if "channels" not in obs.masked:
        lines.append(f"New messages for you: {obs.new_message_count}.")
    if obs.recent_messages is None:  # an episode stored before 0.23.0
        lines += [f"Message in {m.channel_id} from {m.sender}: {m.text}" for m in obs.new_messages or []]
    for m in obs.recent_messages or []:
        who = "you" if m.sender == me else m.sender
        new = m.deliver_t == obs.t and m.sender != me
        lines.append(f"{'Message' if new else f'Earlier message (round {m.t})'} in {m.channel_id} from {who}: {m.text}")
    if obs.news is None:
        lines += [f"News: {h}" for h in obs.headlines]
    lines += [f"News: {n.text}" if n.t == obs.t else f"Earlier news (round {n.t}): {n.text}" for n in obs.news or []]
    lines += [f"Your notes: {n}" for n in notes]
    channel = text_channel(purpose, list(postable))
    if channel:
        lines.append(f"Your reply is posted verbatim to channel {channel}. Reply {PASS} to stay silent.")
        return "\n".join(lines)
    if postable and purpose != "act":
        lines.append(f"Channels you can post to: {', '.join(postable)}.")
    instruction = INSTRUCTION[purpose]
    lines.append(LOOKUP + instruction[0].lower() + instruction[1:] if lookups and purpose == "act" else instruction)
    return "\n".join(lines)


def _with_channels(schema: dict, channels: list[str] | None) -> dict:
    """The tool's input schema with channel_id limited to the seller's channel ids, so the model sees the valid ids."""
    if channels is None:
        return schema
    props = schema["properties"]
    return {**schema, "properties": {**props, "channel_id": {**props["channel_id"], "enum": list(channels)}}}


def _follow_up(messages: list[dict], reply: Reply, results: list, with_tools: bool) -> list[dict]:
    """The history for the next request of a turn. A request with no tools carries no tool call and no tool message
    (some providers reject them without `tools`): the results go in one plain user message instead."""
    if with_tools:
        return (
            messages
            + [{"role": "assistant", "content": reply.text, "tool_calls": [c.__dict__ for c in reply.tool_calls]}]
            + [{"role": "tool", "tool_call_id": c.id, "name": c.name, "content": _canonical(r)} for c, r in results]
        )
    note = "\n".join(f"{c.name}: {r['reason'] if isinstance(r, dict) and 'reason' in r else _canonical(r)}" for c, r in results)
    last = messages[-1]
    if reply.text and reply.text.strip():
        return messages + [{"role": "assistant", "content": reply.text, "tool_calls": []}, {"role": "user", "content": note}]
    if last["role"] != "user":
        return messages + [{"role": "user", "content": note}]
    return messages[:-1] + [{**last, "content": f"{last['content']}\n\n{note}"}]  # keeps the roles alternating


@dataclass
class _TurnStats:
    """What one seller did in one round, for its acted event. Summed over the talk and act phases."""

    steps: int = 0
    tool_calls: int = 0
    messages: int = 0
    limits_hit: list[LimitName] = field(default_factory=list)

    def hit(self, name: LimitName) -> None:
        if name not in self.limits_hit:
            self.limits_hit.append(name)


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    parameters: dict

    def wire(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class LLMPolicy:
    kind = "llm"

    def __init__(
        self,
        seller_id: str,
        endpoint: Endpoint,
        system_prompt: str,
        capability: Capability,
        recorder: Recorder,
        cache: CallCache,
        budget: BudgetGuard,
        temperature: float = 0.7,
        run_seed: int = 0,
        sample_index: int = 0,
        top_p: float | None = None,
        max_output_tokens: int | None = 400,
        max_steps: int = 5,
        max_messages: tuple[int, LimitName] = (1, "max_messages_per_round"),
        window: RateWindow | None = None,
        memory_every: int = 5,
        memory_keep: int = 3,
        memory_summary: str = "code",
        talk_prompt: str | None = None,
        thinking: bool | str | None = None,
        thinking_on_timeout: bool | str | None = None,
        facts: str | None = None,
    ) -> None:
        """`talk_prompt`: system prompt of the talk call (C3: the talk module has its own instruction); None uses system_prompt.
        `max_steps`, `max_messages`: the run limits (RunLimits.messages gives the binding message limit and its name).
        `window`: the profile's 5-hour call window, shared by every run of the data root.
        `max_output_tokens`: None sends no limit (DECISIONS-18 entry 2). `thinking`: Pydantic AI's unified setting, None
        sends nothing (DECISIONS-18 entry 3). `thinking_on_timeout`: the setting of the attempt after a timeout; None keeps
        `thinking` (DECISIONS-22 entry 5.3). `facts`: the market_facts passage, added to every system prompt; None
        adds nothing."""
        if facts is not None:
            system_prompt = f"{system_prompt}\n\n{facts}"
            talk_prompt = f"{talk_prompt}\n\n{facts}" if talk_prompt else None
        self.seller_id, self.endpoint, self.system_prompt, self.talk_prompt = seller_id, endpoint, system_prompt, talk_prompt
        self.capability, self.rec, self.cache, self.budget = capability, recorder, cache, budget
        self.temperature, self.run_seed, self.sample_index = temperature, run_seed, sample_index
        self.top_p, self.max_output_tokens, self.thinking = top_p, max_output_tokens, thinking
        self.thinking_on_timeout = thinking_on_timeout
        self._lowers: bool | None = None  # whether thinking_on_timeout sends other reasoning fields than thinking
        self._ended: str | None = None  # why the last _ask returned None (a FallbackReason)
        self._largest: tuple[float, int] | None = None  # the largest hold of one request so far (usd, tokens)
        self.max_steps, self.memory_every, self.memory_keep = max_steps, memory_every, memory_keep
        self.max_messages, self.window = max_messages, window
        self.memory_summary = memory_summary
        self._stats: dict[int, _TurnStats] = {}
        self.choices: dict[int, str] = {}
        self.profits: dict[int, float] = {}
        self.notes: list[str] = []

    # AgentPolicy

    def talk(self, obs: Observation, tools: ToolBox) -> list[Post]:
        if self.capability.grant("post") is None:
            return []
        return self._loop(obs, tools, "talk")[0]

    def act(self, obs: Observation, tools: ToolBox) -> Action:
        return self._loop(obs, tools, "act")[1]

    def turn(self, obs: Observation, tools: ToolBox) -> tuple[list[Post], Action]:
        return self._loop(obs, tools, "one_call")

    def turn_stats(self, t: int) -> dict | None:
        """The counts of round t for its acted event; read once."""
        st = self._stats.pop(t, None)
        return None if st is None else st.__dict__

    def after_round(self, t: int) -> str | None:
        if self.memory_summary == "none" or t % self.memory_every:  # none: the observation holds the whole record (D15 §1)
            return None
        span = range(max(1, t - self.memory_every + 1), t + 1)
        chosen = ", ".join(self.choices.get(r, "none") for r in span)
        earned = ", ".join(_num(self.profits[r]) for r in span if r in self.profits)
        summary = f"Rounds {span.start}-{t}: your price choices {chosen}." + (f" Profits you saw: {earned}." if earned else "")
        if self.memory_summary == "llm":
            summary = self._llm_note(t, summary) or summary
        self.notes = (self.notes + [summary])[-self.memory_keep :]
        return summary, span.start

    def dump(self) -> dict:
        return {
            "choices": {str(k): v for k, v in self.choices.items()},
            "profits": {str(k): v for k, v in self.profits.items()},
            "notes": self.notes,
        }

    def load(self, state: dict) -> None:
        self.choices = {int(k): v for k, v in state.get("choices", {}).items()}
        self.profits = {int(k): v for k, v in state.get("profits", {}).items()}
        self.notes = list(state.get("notes", []))

    def _llm_note(self, t: int, facts: str) -> str | None:
        """memory_summary = llm: the model writes its own note from the same facts. None on failure (code note is kept)."""
        lines = [f"Earlier note: {n}" for n in self.notes] + [facts, SUMMARY_INSTRUCTION]
        ask = "\n".join(lines)
        req = Request([{"role": "system", "content": self.system_prompt}, {"role": "user", "content": ask}], [], True)
        reply = self._ask(t, "summary", req)
        return reply.text.strip() if reply and reply.text else None

    def phase_need(self, steps: int) -> tuple[int, float | None, int | None]:
        """The worst case of one phase call of `steps` tool steps (SellerPlan.steps), as (requests, usd, tokens): every
        step with every attempt, at the largest request so far; usd and tokens are None before the first request. Run
        uses it to call in parallel only with room for all (DECISIONS-22 5.6; CR-88 addendum 3 point 3)."""
        n = steps * ATTEMPTS
        if self._largest is None:
            return n, None, None
        return n, n * self._largest[0], n * self._largest[1]

    # Preview (POST /prompts/preview): the exact first request body of a phase, sent to a capture transport only.

    def preview(self, obs: Observation, tools: ToolBox, purpose: CallPurpose) -> dict:
        bodies: list[dict] = []

        def capture(request: httpx2.Request) -> httpx2.Response:
            bodies.append(json.loads(request.content))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n")

        wire = self.endpoint.profile.model_copy(update={"protocol": "openai_chat"})  # the preview shows the openai_chat wire body
        probe = Endpoint(wire, api_key="preview-no-key", transport=httpx2.MockTransport(capture))
        request, _ = self._first_request(obs, tools, purpose)
        try:
            probe.request(self._with_settings(request, obs.t, 1))
        except EndpointError:  # the empty stream has no reply; only the captured body is wanted
            pass
        return bodies[0]

    # The loop

    def _tools(self, tools: ToolBox, purpose: CallPurpose) -> tuple[list[_Tool], set[str]]:
        channels = {"post": tools.postable, "read_channel": tools.readable}
        # The call plan decides the offer: its reads while a query is left, the other tools, the writes
        plan = tools.spec
        specs = {s.id: s for s in TOOLS.specs()}
        offered = [*(plan.offered if tools.queries_left else ()), *plan.other_tools]
        reads = [_Tool(t, specs[t].description, _with_channels(specs[t].input_schema, channels.get(t))) for t in offered]
        writes = [_Tool(w, specs[w].description, _with_channels(specs[w].input_schema, channels.get(w))) for w in plan.writes]
        return reads + writes, {r.name for r in reads}

    def _first_request(self, obs: Observation, tools: ToolBox, purpose: CallPurpose) -> tuple[Request, set[str]]:
        offered, reads = self._tools(tools, purpose)
        system = self.talk_prompt if purpose == "talk" and self.talk_prompt else self.system_prompt
        user = render(obs, self.notes, purpose, tools.postable, lookups=bool(reads), me=self.seller_id)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return Request(messages=messages, tools=[t.wire() for t in offered], allow_text=purpose == "talk"), reads

    def _with_settings(self, req: Request, t: int, attempt: int, lowered: bool = False) -> Request:
        settings = {"temperature": self.temperature}
        if self.max_output_tokens is not None:
            settings["max_tokens"] = self.max_output_tokens
        if self.top_p is not None:
            settings["top_p"] = self.top_p
        if self.endpoint.profile.supports_seed:
            settings["seed"] = request_seed(self.run_seed, self.seller_id, t, self.sample_index, attempt)
        thinking = self.thinking_on_timeout if lowered and self.thinking_on_timeout is not None else self.thinking
        if thinking is not None:
            settings["thinking"] = thinking
        return Request(req.messages, req.tools, req.allow_text, settings)

    def _loop(self, obs: Observation, tools: ToolBox, purpose: CallPurpose) -> tuple[list[Post], Action | None]:
        if obs.own.last_profit is not None:
            self.profits[obs.t - 1] = obs.own.last_profit
        req, reads = self._first_request(obs, tools, purpose)
        st = self._stats.setdefault(obs.t, _TurnStats())
        limit, limit_name = self.max_messages
        posts: list[Post] = []
        channel = tools.spec.text_channel
        queries = {s.id for s in tools.specs if s.counts_as_query}
        granted = queries if tools.query_limit else set()
        reason = "max_steps"
        for _ in range(self.max_steps):
            reply = self._ask(obs.t, purpose, req, granted)
            if reply is None:
                reason = self._ended
                if purpose == "talk":
                    if reason == "timeout":
                        self._replied(obs.t, "pass", channel, reason="timeout")
                    else:
                        self._replied(obs.t, "error", channel)
                break
            st.steps += 1
            st.tool_calls += len(reply.tool_calls)
            if not reply.tool_calls:  # only a talk call accepts text
                said = (reply.text or "").strip()
                if not channel:
                    outcome = "pass"
                elif said.rstrip(".").upper() == PASS:
                    outcome = "pass"
                elif not said:
                    outcome = "empty"
                elif st.messages >= limit:
                    st.hit(limit_name)
                    outcome = "refused"
                else:
                    posts.append(Post(channel_id=channel, text=said))
                    st.messages += 1
                    self._replied(obs.t, "posted", channel, posted=said)
                    return posts, None
                self._replied(obs.t, outcome, channel, unposted=reply.text)
                return posts, None
            action, results = None, []
            for c in reply.tool_calls:
                args = json.loads(c.arguments or "{}")
                if c.name in reads:
                    out = tools.call(c.name, **args)
                    refused = NO_LOOKUP if c.name in queries and not tools.queries_left else {"refused": True}
                    results.append((c, out if out is not None else refused))
                elif c.name in granted:  # a read this call did not offer: a budget refusal (DECISIONS-12 §2a)
                    tools.refuse(c.name, args)
                    results.append((c, NO_LOOKUP))
                elif c.name == "post" and st.messages >= limit:
                    st.hit(limit_name)
                    results.append((c, {"refused": True, "reason": f"at most {limit} message(s) per round"}))
                elif c.name == "post" and args["channel_id"] not in tools.postable:
                    reason = f"no channel {args['channel_id']}; post to one of: {', '.join(tools.postable)}"
                    results.append((c, {"refused": True, "reason": reason}))
                elif c.name == "post":
                    posts.append(Post(**args))
                    st.messages += 1
                    results.append((c, {"posted": True, "delivered_next_round": True}))
                elif c.name == "set_price":
                    action = Action(price=args["price"], plan=args.get("plan"))
                elif c.name == "hold":
                    action = Action(hold=True, plan=args.get("plan"))
            if purpose == "talk" and posts:
                self._replied(obs.t, "posted", None, posted="\n".join(p.text for p in posts), unposted=reply.text)
            if action is not None or (purpose == "talk" and posts):
                self.choices[obs.t] = (
                    "hold" if action and action.hold else _num(action.price) if action else self.choices.get(obs.t, "none")
                )
                return posts, action
            if purpose == "talk":  # text beside a tool call that did not post: tier B (CR-46 n3)
                self._unposted(obs.t, reply.text)
            offered = req.tools if tools.queries_left else [t for t in req.tools if t["name"] not in queries]
            req = Request(_follow_up(req.messages, reply, results, bool(offered)), offered, req.allow_text)
        else:
            st.hit("max_steps_per_turn")
            if purpose == "talk":
                self._replied(obs.t, "max_steps", channel)
        if purpose == "talk":
            return posts, None
        self.choices[obs.t] = "hold"
        return posts, Action(hold=True, fallback=True, fallback_reason=reason)

    def _replied(
        self, t: int, outcome: str, channel: str | None, posted: str | None = None, unposted: str | None = None, reason: str | None = None
    ) -> None:
        """Tier A gets the outcome and only posted text; reply text that was not posted goes to tier B (talk_text)."""
        self.rec.emit(TalkRepliedEvent, t, agent_id=self.seller_id, channel_id=channel, outcome=outcome, text=posted, reason=reason)
        self._unposted(t, unposted)

    def _unposted(self, t: int, text: str | None) -> None:
        if text and text.strip():
            self.rec.emit(TalkTextEvent, t, agent_id=self.seller_id, text=text)

    def _valid(self, reply: Reply, req: Request, granted: set[str] = frozenset()) -> str | None:
        """None if the reply is usable, else the llm_call error code. A granted read that is not offered is usable: the
        loop records it as a budget refusal."""
        offered = {t["name"] for t in req.tools} | granted
        if not reply.tool_calls:
            return None if req.allow_text else "invalid_tool"
        for c in reply.tool_calls:
            if c.name not in offered:
                return "invalid_tool"
            try:
                TOOLS[c.name].input_model.model_validate_json(c.arguments or "{}")
            except ValidationError:
                return "parse_error"
        return None

    def _ask(self, t: int, purpose: CallPurpose, base: Request, granted: set[str] = frozenset()) -> Reply | None:
        """Up to ATTEMPTS calls; each attempt is one llm_call event. A fatal endpoint error is raised after its event.
        DECISIONS-22 entry 5.4: a slow call (SLOW) sends the next attempt with thinking_on_timeout (degraded when that
        level is set and differs); a second slow call ends the call with None and self._ended timeout. A text-only act
        reply gets the NUDGE request, only while an attempt is left; an invalid reply or another failure is resent
        unchanged at the same level."""
        profile = self.endpoint.profile
        lowered = slow = False
        for attempt in range(1, ATTEMPTS + 1):
            req = self._with_settings(base, t, attempt, lowered)
            canonical, key = self._cache_key(req, t, attempt)
            got = self._fetch(t, req, canonical, key)
            reply, error = got.reply, got.error
            if reply is not None:
                error = self._valid(reply, req, granted)
            self.rec.emit(
                LlmCallEvent,
                t,
                agent_id=self.seller_id,
                purpose=purpose,
                request_hash=key,
                sample_index=self.sample_index,
                cache_hit=got.cache_hit,
                charged=got.charged,
                attempt=attempt,
                provider_profile_id=profile.id,
                model=profile.model,
                input_tokens=reply.input_tokens if reply else None,
                output_tokens=reply.output_tokens if reply else None,
                cached_input_tokens=reply.cached_input_tokens if reply else None,
                reasoning_tokens=reply.reasoning_tokens if reply else None,
                cost_usd=got.cost_usd,
                latency_ms=got.latency_ms,
                error=error,
                http_status=got.http_status,
                provider_code=got.provider_code,
                provider_message=got.provider_message,
                degraded=True if lowered and self._degrades() else None,
                slot_wait_ms=got.slot_wait_ms,
                first_byte_ms=got.first_byte_ms,
                settings=self.endpoint.sent_settings(req.settings),
            )
            if got.fatal:
                raise got.fatal
            if got.http_status == 429:  # the provider's quota or rate limit: stop the run, never fill it with fallbacks (24 Oct)
                raise RateWindowFull("provider_429")
            if error is None:
                return reply
            slow = reply is None and got.provider_code in SLOW
            if slow:
                if lowered:
                    self._ended = "timeout"
                    return None
                lowered = True
                continue
            if reply is not None and not reply.tool_calls and base.messages[-1]["content"] != NUDGE:
                nudge = [{"role": "assistant", "content": reply.text or "", "tool_calls": []}, {"role": "user", "content": NUDGE}]
                base = Request(base.messages + nudge, base.tools, base.allow_text)
        # no attempt left: a last timeout had no retry; an invalid reply, or a provider error
        self._ended = "timeout" if slow else "invalid" if error in ("invalid_tool", "parse_error") else "error"
        return None

    def _degrades(self) -> bool:
        """True when the lowered attempt sends other reasoning fields than the first (Endpoint.sent_thinking), so a level
        that the provider maps to the same field (gpt-6-luna: minimal is low) is not degraded (lead, 23 Oct)."""
        if self._lowers is None:
            low = self.thinking_on_timeout
            self._lowers = low is not None and self.endpoint.sent_thinking(low) != self.endpoint.sent_thinking(self.thinking)
        return self._lowers

    def _cache_key(self, req: Request, t: int, attempt: int) -> tuple[str, str]:
        profile = self.endpoint.profile
        canonical = _canonical(
            {
                **req.canonical(),
                "model": profile.model,
                "host": profile.host,
                "run_seed": self.run_seed,
                "seller": self.seller_id,
                "t": t,
                "sample_index": self.sample_index,
                "attempt": attempt,
            }
        )
        return canonical, "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _fetch(self, t: int, req: Request, canonical: str, key: str) -> "_Fetched":
        """The cached reply, or one paid request inside the budget. Budget warnings are recorded here."""
        cached = self.cache.get(key)
        if cached is not None:
            got = _Fetched(reply=cached.reply, cache_hit=True)
            if cached.paid_by_run == self.rec.run_id:
                # This run paid for it in a round that was lost (crash or fatal error): count the cost again, once.
                got.cost_usd, got.charged = cached.cost_usd, True
                for level in self.budget.charge(got.cost_usd, cached.reply.tokens):
                    self.rec.emit(BudgetWarningEvent, t, spent_usd=self.budget.spent_usd, max_usd=self.budget.max_usd or 0.0, level=level)
            return got
        profile = self.endpoint.profile
        recorded = self.cache.timed_out(key)
        if recorded is not None:  # a slow call of this request key is an outcome, replayed with no call (D22 entry 5.5)
            return _Fetched(cache_hit=True, error="timeout" if recorded == "call_timeout" else "provider_error", provider_code=recorded)
        size = len(canonical) // 3
        try:
            most = self.max_output_tokens or OPEN_OUTPUT_TOKENS
            hold = self.budget.reserve(profile.cost(size, most), size + most)
            usd, tokens = self._largest or (0.0, 0)
            self._largest = max(usd, hold.usd), max(tokens, hold.tokens)
        except BudgetExceeded:
            self.rec.emit(BudgetWarningEvent, t, spent_usd=self.budget.spent_usd, max_usd=self.budget.max_usd or 0.0, level="p100")
            raise
        if self.window is not None:
            try:
                self.window.take()
            except RateWindowFull:  # no call is made: neither the budget nor its warnings count it
                self.budget.cancel(hold)
                raise
        got = _Fetched()
        try:
            got.reply, got.latency_ms = self.endpoint.request(req)
        except EndpointError as e:
            got.error, got.fatal = e.code, e if e.fatal else None
            got.http_status, got.provider_code, got.provider_message = e.http_status, e.provider_code, e.message
        except BaseException:
            self.budget.spend(0.0, 0, hold)
            raise
        finally:
            got.slot_wait_ms, got.first_byte_ms = self.endpoint.timing()
        tokens = 0
        if got.reply is not None:
            r = got.reply
            got.cost_usd = profile.cost(r.input_tokens or 0, r.output_tokens or 0, r.cached_input_tokens or 0)
            tokens = r.tokens
        elif not profile.priced:
            got.cost_usd = None
        try:
            if got.reply is not None:
                self.cache.put(key, canonical, got.reply, profile.model, profile.host, got.cost_usd, self.rec.run_id)
            else:
                self.cache.put_failed(key, canonical, profile.model, profile.host, self.rec.run_id)
                if got.provider_code in SLOW:
                    self.cache.put_timeout(key, canonical, profile.model, profile.host, got.provider_code, self.rec.run_id)
        finally:  # the call is paid even when the cache write fails: count it and free its hold
            levels = self.budget.spend(got.cost_usd, tokens, hold)
        for level in levels:
            self.rec.emit(BudgetWarningEvent, t, spent_usd=self.budget.spent_usd, max_usd=self.budget.max_usd or 0.0, level=level)
        return got
