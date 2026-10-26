"""The run loop (synchronous rounds). The host drives time and delivers frozen news; it decides no outcome."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import field_validator, model_validator

from simcore import __version__
from simcore.agents import Action, AgentPolicy, BudgetExceeded, CapabilityError, Post
from simcore.callplan import CallMode, CallPlan, Setting, call_plan, open_channels
from simcore.market import EPS
from simcore.recorder import Recorder
from simcore.roles import PHASES, ROLES, Phase
from simcore.scenario import Event, Scenario
from simcore.state import Mask, Message, NewsItem, Outcome, PricePoint, RunState
from simcore.tools import TOOLS
from simcore.view import ObservationMode, Pricing, QueryBudget, ToolBox, View, result_hash
from simschema.base import Frozen
from simschema.events import (
    AblationIntervention,
    ActedEvent,
    AgentHeader,
    BranchInfo,
    BudgetWarningEvent,
    ChannelInfo,
    EpisodeHeader,
    IntervenedEvent,
    Labels,
    MemorySummarisedEvent,
    NewsPublishedEvent,
    ObservedEvent,
    PlannedEvent,
    PostedEvent,
    ProbeIntervention,
    Reference,
    ReferenceEvent,
    ReferencePoint,
    RoundStartedEvent,
    RunEndedEvent,
    RunLimits,
    RunStartedEvent,
    RunStatusChangedEvent,
    ScenarioRef,
    SettledEvent,
    ToolCalledEvent,
)

HOST_KINDS = {"scripted_host"}
LOOP_EVENTS = frozenset({"run_started", "run_status_changed", "run_ended", "snapshot_saved"})


class HostPolicy(Protocol):
    kind: str

    def news_text(self, t: int, event_index: int, event: Event) -> str: ...


class RunControl(Protocol):
    """Asked at each round boundary. wait() blocks while paused and returns "go", "stop" or "shutdown"."""

    def check(self, t: int) -> Literal["go", "pause", "stop", "shutdown"]: ...

    def wait(self) -> Literal["go", "stop", "shutdown"]: ...


class RunInterrupted(BaseException):
    """The server shuts down. Not an outcome: no run_ended is written, so the durable workflow stays PENDING and resumes.

    A BaseException (like SystemExit), so neither the run service nor DBOS records it as a failure.
    """


class ScriptedHost:
    """The default host: publishes the frozen news text unchanged."""

    kind = "scripted_host"

    def news_text(self, t: int, event_index: int, event: Event) -> str:
        return event.news


@dataclass
class RoundContext:
    """What the phases of one round share."""

    t: int
    params: object
    ref: Reference
    point: ReferencePoint
    forced: dict[str, float]
    budgets: dict[str, QueryBudget]
    prices: dict[str, float] = field(default_factory=dict)


class PhaseAgent(Protocol):
    """An agent of a role that has no built-in handler. The loop calls it in each phase its role lists."""

    def run_phase(self, phase: Phase, ctx: RoundContext) -> None: ...


class RunSpec(Frozen):
    run_id: str
    batch_id: str | None = None
    kind: Literal["research", "exploratory"]
    split: Literal["pilot", "dev", "test", "exploratory"]
    seed: int
    condition_id: str | None = None
    condition_hash: str | None = None
    labels: Labels | None = None
    parent_labels: Labels | None = None
    seeds_hash: str | None = None
    agents: list[AgentHeader]
    public: bool = False
    private_pairs: bool = False
    call_mode: CallMode = "talk_and_act"
    c3_isolation: bool = False
    memory_summary: Literal["code", "llm", "none"] = "code"
    observation_mode: ObservationMode = "both"
    limits: RunLimits = RunLimits()
    preset_id: str | None = None
    config_hash: str | None = None
    cloned_from: str | None = None
    created_at: str
    code_commit: str | None = None

    @field_validator("agents")
    @classmethod
    def _agents(cls, agents: list[AgentHeader]) -> list[AgentHeader]:
        out = []
        for a in agents:
            if a.role not in ROLES:
                raise ValueError(f"unknown role {a.role}")
            role = ROLES[a.role]
            if not role.built or role.stage != "run":
                raise ValueError(f"role {a.role} cannot take part in a run (designed, not built, or evaluation stage)")
            if a.role == "host" and a.policy_kind not in HOST_KINDS:
                raise ValueError(f"host policy {a.policy_kind} is not built")
            out.append(a.model_copy(update={"capability": TOOLS.normalise(a.capability, a.role, role.tools)}))
        ids = [a.id for a in out]
        if len(set(ids)) != len(ids):
            raise ValueError("agent ids must be distinct")
        if sum(a.role == "host" for a in out) > 1:
            raise ValueError("a run has at most one host")
        return out

    @model_validator(mode="after")
    def _rules(self) -> "RunSpec":
        research = (self.labels is not None or self.parent_labels is not None, self.condition_id is not None, self.split != "exploratory")
        if self.labels is not None and self.parent_labels is not None:
            raise ValueError("a run has its own labels or its parent's labels (ablation child), not both")
        if self.parent_labels is not None and (self.condition_id is not None or self.condition_hash is not None):
            raise ValueError("an ablation child has left its parent's condition: condition_id and condition_hash are null")
        if self.kind == "research":
            own = self.labels is not None and self.condition_id is not None
            if not ((own or self.parent_labels is not None) and self.split != "exploratory"):
                raise ValueError("a research run needs a condition, its labels and a pilot, dev or test split")
            for a in self.agents:
                role = ROLES[a.role]
                if not role.research_allowed or (role.research_scripted_only and a.policy_kind.startswith("llm")):
                    raise ValueError(f"agent {a.id} ({a.role}, {a.policy_kind}) is not allowed in a research run")
        elif any(research):
            raise ValueError("an exploratory run has no condition, no labels and split exploratory")
        if len(self.sellers()) < 2:
            raise ValueError("a run needs at least two sellers")
        if refused := self.plan().refused:
            raise ValueError(refused[0])
        return self

    def plan(self) -> CallPlan:
        """The call plan of this run (simcore.callplan): the header holds the effective grants, so news and transparency
        trim nothing more."""
        caps = {a.id: a.capability for a in self.sellers()}
        return call_plan(
            Setting(
                caps, tuple(self.channels()), self.call_mode, self.c3_isolation, self.observation_mode, self.limits, self.memory_summary
            )
        )

    def sellers(self) -> list[AgentHeader]:
        return [a for a in self.agents if a.role == "seller"]

    def channels(self) -> list[ChannelInfo]:
        return list(open_channels([s.id for s in self.sellers()], self.public, self.private_pairs))

    def seller_order(self) -> list[str]:
        order = sorted(s.id for s in self.sellers())
        random.Random(f"order:{self.seed}").shuffle(order)
        return order


@contextmanager
def seller_call(phase: Phase, s: str, t: int):
    """An exception raised in a seller's call leaves with `sim_where` (phase, seller, round): run_ended.error_detail.where.
    The innermost call sets it."""
    try:
        yield
    except Exception as e:
        if getattr(e, "sim_where", None) is None:
            e.sim_where = {"phase": phase, "seller_id": s, "t": t}
        raise


def _close_loop() -> None:
    """Close the event loop of this seller thread, which its model client used, before the thread ends."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:  # the thread made no request
        return
    if not loop.is_closed():
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


ROOM_MARGIN = 1.25
"""The parallel check sizes each request at 1.25 times the largest so far: the record grows each round (CR-90 minor 1)."""


class Run:
    def __init__(
        self,
        spec: RunSpec,
        scenario: Scenario,
        policies: dict[str, AgentPolicy],
        recorder: Recorder,
        *,
        host: HostPolicy | None = None,
        state: RunState | None = None,
        branch: BranchInfo | None = None,
        rounds: range | None = None,
        control: RunControl | None = None,
        agents: dict[str, PhaseAgent] | None = None,
    ) -> None:
        """`policies`: one per seller. `agents`: one per agent of any other role except the host."""
        if scenario.status != "frozen":
            raise ValueError("a run reads only a frozen scenario")
        if scenario.hash != scenario.content_hash():
            raise ValueError("the scenario hash does not match its content")
        if spec.kind == "research":
            if not scenario.market.demand.spec.research_allowed:
                raise ValueError(
                    f"demand model {scenario.market.demand.id} has no reference values, so it is not allowed in a research run"
                )
            if host is not None and type(host) is not ScriptedHost:
                raise ValueError("a research run uses only the scripted host")
        sellers = sorted(s.id for s in spec.sellers())
        if sorted(policies) != sellers or sellers != sorted(scenario.seller_ids):
            raise ValueError("policies, run sellers and scenario sellers must name the same sellers")
        others = [a for a in spec.agents if a.role not in ("seller", "host")]
        if sorted(a.id for a in others) != sorted(agents or {}):
            raise ValueError("every agent that is not a seller or the host needs a phase agent")
        self.spec, self.scenario, self.policies, self.rec = spec, scenario, policies, recorder
        self.agents = agents or {}
        self.host = host or ScriptedHost()
        self.market = scenario.market.build()
        self.sellers = sellers
        self.order = spec.seller_order()
        self.caps = {s.id: s.capability for s in spec.sellers()}
        self.channels = spec.channels()
        base = scenario.market.demand.spec.parse(scenario.market.demand.params)
        pricing = Pricing(
            tuple(sellers),
            lambda prices: (lambda s: (s.prices, s.quantities, s.profits))(
                self.market.settle({k: self.market.grid.snap(v) for k, v in prices.items()}, base)
            ),
        )
        self.view = View(
            self.channels, self.caps, spec.c3_isolation, pricing, spec.observation_mode, spec.call_mode, spec.limits, spec.memory_summary
        )
        self.branch = branch
        self.control = control
        self.state = state or RunState()
        self.rounds = rounds or range(1, scenario.horizon.stop_round(spec.seed) + 1)
        for s, blob in self.state.policy_state.items():
            policies[s].load(blob)
        host_id = next((a.id for a in spec.agents if a.role == "host"), "host")
        cast = [(host_id, "host"), *((s, "seller") for s in self.order), *((a.id, a.role) for a in others)]
        self.plan: dict[Phase, list[tuple[str, str]]] = {p: [(i, r) for i, r in cast if p in ROLES[r].phases] for p in PHASES}
        self.handlers: dict[str, Callable[[Phase, str, RoundContext], None]] = {"host": self._host_phase, "seller": self._seller_phase}
        self._acting: str | None = None
        recorder.guard = self._check_event
        # DECISIONS-22 entry 5.6: LLM sellers call in parallel within a phase, each on its own thread for the whole run
        self._parallel = any(getattr(p, "kind", None) == "llm" for p in policies.values())
        self._pins: dict[str, ThreadPoolExecutor] = {}
        self._plan = spec.plan()

    def header(self) -> EpisodeHeader:
        sp = self.spec
        return EpisodeHeader(
            run_id=sp.run_id,
            batch_id=sp.batch_id,
            kind=sp.kind,
            condition_id=sp.condition_id,
            condition_hash=sp.condition_hash,
            labels=sp.labels,
            parent_labels=sp.parent_labels,
            label_source="condition_config" if sp.labels else None,
            split=sp.split,
            seed=sp.seed,
            seeds_hash=sp.seeds_hash,
            scenario=ScenarioRef(
                id=self.scenario.id,
                hash=self.scenario.hash,
                demand_model=self.scenario.market.demand.id,
                demand_version=self.scenario.market.demand.version,
            ),
            branch=self.branch,
            agents=sp.agents,
            seller_order=self.order,
            channels=self.channels,
            call_mode=sp.call_mode,
            c3_isolation=sp.c3_isolation,
            memory_summary=sp.memory_summary,
            observation_mode=sp.observation_mode,
            limits=sp.limits,
            max_rounds=self.rounds.stop - 1,
            reproducible=self.host.kind == "scripted_host",
            preset_id=sp.preset_id,
            config_hash=sp.config_hash,
            cloned_from=sp.cloned_from,
            simcore_version=__version__,
            code_commit=sp.code_commit,
            created_at=sp.created_at,
        )

    def execute(self, resume: bool = False) -> RunState:
        """resume=True continues a stored run from its last snapshot (self.state, self.rounds set by the caller)."""
        try:
            return self._execute(resume)
        finally:
            for pin in self._pins.values():
                pin.submit(_close_loop).result()
                pin.shutdown(wait=True)

    def _execute(self, resume: bool) -> RunState:
        start = self.rounds.start - 1
        if resume:
            self.rec.emit(RunStatusChangedEvent, start, status="running", reason="recovered")
        else:
            self.rec.emit(RunStartedEvent, start, header=self.header())
            self.rec.emit(RunStatusChangedEvent, start, status="running")
        if self.branch and isinstance(self.branch.intervention, AblationIntervention) and self.branch.intervention.ablated == "channel":
            ab = self.branch.intervention
            mask = Mask(channel_id=ab.channel_id, mode=ab.mode, from_t=self.branch.fork_t)
            if mask not in self.state.masks:
                self.state.masks.append(mask)
        for t in self.rounds:
            if not self._go_on(t):
                self.rec.emit(RunEndedEvent, t - 1, status="stopped", end_reason="stopped_by_user", episode_hash=self.rec.episode_hash)
                return self.state
            try:
                self._round(t)
            except BudgetExceeded as e:
                self.rec.emit(RunEndedEvent, t, status="stopped", end_reason=e.end_reason, episode_hash=self.rec.episode_hash)
                return self.state
        self.rec.emit(RunEndedEvent, self.rounds.stop - 1, status="completed", end_reason="horizon", episode_hash=self.rec.episode_hash)
        return self.state

    def _go_on(self, t: int) -> bool:
        decision = self.control.check(t) if self.control else "go"
        if decision == "pause":
            self.rec.emit(RunStatusChangedEvent, t - 1, status="paused")
            decision = self.control.wait()
            if decision == "go":
                self.rec.emit(RunStatusChangedEvent, t - 1, status="running")
        if decision == "shutdown":
            raise RunInterrupted
        return decision == "go"

    def _check_event(self, kind: str) -> None:
        allowed = LOOP_EVENTS if self._acting is None else ROLES[self._acting].output_events
        if kind not in allowed:
            raise RuntimeError(f"event {kind} is not an output of role {self._acting or 'run loop'}")

    def _round(self, t: int) -> None:
        params = self.scenario.params(t)
        ref = self.market.reference(params, self.sellers)
        point = ReferencePoint(
            t=t, params=params.model_dump(mode="json"), costs={s: params.cost_of(s) for s in self.sellers}, **ref.model_dump()
        )
        ctx = RoundContext(
            t=t,
            params=params,
            ref=ref,
            point=point,
            forced=self._forced(t, ref),
            budgets={s: QueryBudget(self.spec.limits.queries(self.caps[s].max_queries_per_round)[0]) for s in self.sellers},
        )
        for phase in PHASES:
            group = [i for i, r in self.plan[phase] if r == "seller"] if self._parallel and phase in ("talk", "act") else []
            for agent_id, role in self.plan[phase]:
                if agent_id in group[1:]:
                    continue
                self._acting = role
                try:
                    handler = self.handlers.get(role)
                    if agent_id in group:
                        self._sellers(phase, group, ctx)
                    elif handler:
                        handler(phase, agent_id, ctx)
                    else:
                        self.agents[agent_id].run_phase(phase, ctx)
                finally:
                    self._acting = None
        self.state.policy_state = {s: self.policies[s].dump() for s in self.sellers}
        self.rec.snapshot(t, self.state)

    def _host_phase(self, phase: Phase, _id: str, ctx: RoundContext) -> None:
        t = ctx.t
        if phase == "news":
            self.rec.emit(RoundStartedEvent, t, round=t, reference=ctx.point)
            if self.branch and t == self.branch.fork_t:
                iv = self.branch.intervention
                price = ctx.forced.get(iv.target) if isinstance(iv, ProbeIntervention) else None
                self.rec.emit(IntervenedEvent, t, intervention=iv, effective_price=price)
            for i, e in enumerate(self.scenario.events):
                if e.announce == t:
                    text = self.host.news_text(t, i, e)
                    self.state.news.append(NewsItem(t=t, event_index=i, text=text, visible=e.visible))
                    self.rec.emit(NewsPublishedEvent, t, event_index=i, text=text, visible=e.visible)
        elif phase == "settle":
            prices = ctx.prices
            settled = self.market.settle(prices, ctx.params)
            best = {
                s: self.market.best_response(s, {r: self.state.last_price(r).price for r in self.sellers if r != s}, ctx.params)
                for s in self.sellers
                if all(self.state.last_price(r) for r in self.sellers if r != s)
            }
            self.rec.emit(
                SettledEvent, t, prices=settled.prices, quantities=settled.quantities, profits=settled.profits, clamped=settled.clamped
            )
            self.rec.emit(ReferenceEvent, t, reference=ctx.point, best_response=best, consumer_surplus=settled.consumer_surplus)
            for s in self.sellers:
                self.state.history.setdefault(s, []).append(PricePoint(t=t, price=prices[s], forced=s in ctx.forced))
                self.state.outcomes.setdefault(s, []).append(Outcome(t=t, quantity=settled.quantities[s], profit=settled.profits[s]))
            self.state.t = t

    def _seller_phase(self, phase: Phase, s: str, ctx: RoundContext) -> None:
        if phase == "settle":
            with seller_call("settle", s, ctx.t):
                note = self._on_pin(s, self.policies[s].after_round, ctx.t)
            if note is not None:
                summary, first = note
                self.rec.emit(MemorySummarisedEvent, ctx.t, agent_id=s, summary=summary, covers=(first, ctx.t))
            return
        self._apply(phase, s, ctx, self._call(phase, s, ctx))

    def _call(self, phase: Phase, s: str, ctx: RoundContext) -> tuple[str, object] | None:
        """The seller's call of a talk or act phase. It reads the state and changes nothing in it, so the sellers of one
        phase can call at the same time: a post is delivered next round and a price is seen after settlement."""
        t, policy = ctx.t, self.policies[s]
        with seller_call(phase, s, t):
            if phase == "talk" and self.spec.call_mode == "talk_and_act":
                if self.view.talks(s):  # no post grant, no talk turn: no call and no talk observation (CR-57 n1)
                    return "talk", policy.talk(self._observe(s, t, "talk"), self._tools(s, t, "talk", ctx.budgets[s]))
            elif phase == "act" and self.spec.call_mode == "talk_and_act":
                if s in ctx.forced:
                    return "act", None
                return "act", policy.act(self._observe(s, t, "act"), self._tools(s, t, "act", ctx.budgets[s]))
            elif phase == "act":
                return "turn", policy.turn(self._observe(s, t, "one_call"), self._tools(s, t, "one_call", ctx.budgets[s]))
        return None

    def _apply(self, phase: Phase, s: str, ctx: RoundContext, result: tuple[str, object] | None) -> None:
        """What the call changes, in seller order: the posts, the price."""
        if result is None:
            return
        t, (kind, value) = ctx.t, result
        if kind == "talk":
            self._publish(s, t, value)
            return
        posts, action = value if kind == "turn" else ([], value)
        self._publish(s, t, posts)
        ctx.prices[s] = self._act(s, t, None if s in ctx.forced else action, ctx.forced.get(s), ctx.ref, ctx.budgets[s])

    def _pin(self, s: str) -> ThreadPoolExecutor:
        """The seller's own thread for the whole run, so its client keeps one event loop (DECISIONS-22 entry 5.6)."""
        if s not in self._pins:
            self._pins[s] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"seller-{s}")
        return self._pins[s]

    def _on_pin(self, s: str, fn: Callable, *args):
        return self._pin(s).submit(fn, *args).result() if self._parallel else fn(*args)

    def _sellers(self, phase: Phase, ids: list[str], ctx: RoundContext) -> None:
        """DECISIONS-22 entry 5.6: the sellers of a talk or act phase call at the same time. Each one records into its own
        buffer; the buffers are merged in seller order after every call has returned, so the events are in the order of a
        sequential run. Budget warnings are recorded after the phase. A failure is raised after the merge, the first in
        seller order. When the run budget has no room for every seller's worst case, the calls run one after the other
        (same records): so a budget limit always stops the same seller."""

        def call(s: str):
            with self.rec.buffered() as events:
                try:
                    return events, self._call(phase, s, ctx), None
                except BaseException as e:  # noqa: BLE001 (raised after the merge)
                    return events, None, e

        together = self._room(phase, ids)
        path = together if len(self._llm(ids)) > 1 else None  # recorded on each llm_call (CR-88 addendum 3 point 3)
        if together:
            futures = [self._pin(s).submit(call, s) for s in ids]
            done = [f.result() for f in futures]
        else:  # one after the other, as before D22: a failure stops the phase before the next seller's call
            done = []
            for s in ids:
                done.append(self._pin(s).submit(call, s).result())
                if done[-1][2] is not None:
                    break
        error = None
        for s, (events, result, failed) in zip(ids, done):
            if path is not None:
                events = [e.model_copy(update={"parallel": path}) if e.type == "llm_call" else e for e in events]
            self.rec.merge(events)
            if error is None and failed is None:
                self._apply(phase, s, ctx, result)
            error = error or failed
        self._warnings(ctx.t)
        if error is not None:
            raise error

    def _llm(self, ids: list[str]) -> list:
        return [self.policies[s] for s in ids if getattr(self.policies[s], "kind", None) == "llm"]

    def _room(self, phase: Phase, ids: list[str]) -> bool:
        """True when the run budget and the call windows hold the worst case of every LLM seller of the phase: its real
        step count (SellerPlan.steps) times ATTEMPTS requests, each at its largest request so far. Before the first request
        the cost is unknown, so only a budget with no USD or token cap has room."""
        llm = self._llm(ids)
        if len(llm) < 2:
            return True
        purpose = "one_call" if self.spec.call_mode == "one_call" else phase
        needs = []
        for p in llm:
            plan = self._plan.sellers[p.seller_id]
            needs.append(p.phase_need(plan.steps(purpose, p.max_steps)) if purpose in plan.calls else (0, 0.0, 0))
        budget = llm[0].budget
        needs = [(n, None if u is None else u * ROOM_MARGIN, None if k is None else int(k * ROOM_MARGIN)) for n, u, k in needs]
        if any(n[1] is None for n in needs) and (budget.max_usd is not None or budget.max_tokens is not None):
            return False
        if not budget.room(sum(n[0] for n in needs), sum(n[1] or 0.0 for n in needs), sum(n[2] or 0 for n in needs)):
            return False
        windows: dict[int, list] = {}
        for p, n in zip(llm, needs):
            if p.window is not None:
                windows.setdefault(id(p.window), [p.window, 0])[1] += n[0]
        return all(w.room() >= calls for w, calls in windows.values())

    def _warnings(self, t: int) -> None:
        """The budget warnings of the phase, one per level, with what the run spent at its end."""
        deferred, self.rec.deferred = self.rec.deferred, []
        if not deferred:
            return
        spent = self._llm(self.sellers)[0].budget.spent_usd
        for level in sorted({e.level for e in deferred}):
            first = next(e for e in deferred if e.level == level)
            self.rec.emit(BudgetWarningEvent, t, spent_usd=spent, max_usd=first.max_usd, level=level)

    def _forced(self, t: int, ref: Reference) -> dict[str, float]:
        probe = self.branch.intervention if self.branch else None
        if not isinstance(probe, ProbeIntervention) or not self.branch.fork_t <= t < self.branch.fork_t + probe.rounds:
            return {}
        value = ref.nash_by_seller[probe.target] if probe.price == "nash" else probe.price
        return {probe.target: self.market.grid.snap(value)}

    def _observe(self, s: str, t: int, purpose):
        obs = self.view.observe(s, t, self.state, purpose)
        self.rec.emit(ObservedEvent, t, agent_id=s, purpose=purpose, observation=obs)
        return obs

    def _tools(self, s: str, t: int, purpose, budget: QueryBudget) -> ToolBox:
        def record(tool_id, version, arguments, allowed, result):
            private = tool_id in TOOLS and TOOLS[tool_id].private  # the notes: model text stays out of tier A
            self.rec.emit(
                ToolCalledEvent,
                t,
                agent_id=s,
                tool=tool_id,
                tool_version=version,
                arguments={} if private else arguments,
                allowed=allowed,
                result=None if private else result,
                result_hash=result_hash(result) if allowed else None,
            )
            if allowed and tool_id == "write_note":
                self.rec.emit(PlannedEvent, t, agent_id=s, plan=arguments["text"], source="note")

        return self.view.toolbox(s, t, self.state, purpose, budget, record)

    def _publish(self, s: str, t: int, posts: list[Post]) -> None:
        if not posts:
            return
        grant = self.caps[s].grant("post")
        if grant is None:
            raise CapabilityError(f"{s} has no post grant")
        limit, name = self.spec.limits.messages(TOOLS.params(grant).max_per_round)
        sent = sum(m.sender == s and m.t == t for m in self.state.messages)
        if sent + len(posts) > limit:
            raise CapabilityError(f"{s} posted {sent + len(posts)} messages this round; the limit ({name}) is {limit}")
        postable = self.view.postable_channels(s)
        for p in posts:
            if p.channel_id not in postable:  # LLMPolicy refuses such a post as a tool error; a scripted one is a config bug
                raise CapabilityError(f"{s} cannot post to {p.channel_id}")
            deliver_t = t + 1
            self.state.messages.append(
                Message(id=len(self.state.messages), t=t, channel_id=p.channel_id, sender=s, text=p.text, deliver_t=deliver_t)
            )
            # A post in the last round is never delivered: the run has no round deliver_t (CR-50 finding 3).
            receivers = self.view.receivers(s, p.channel_id, deliver_t, self.state) if deliver_t in self.rounds else []
            self.rec.emit(PostedEvent, t, agent_id=s, channel_id=p.channel_id, text=p.text, deliver_t=deliver_t, receivers=receivers)

    def _act(self, s: str, t: int, action: Action | None, forced_price: float | None, ref: Reference, queries: QueryBudget) -> float:
        if forced_price is not None:
            self.rec.emit(ActedEvent, t, agent_id=s, price=forced_price, hold=False, forced=True)
            return forced_price
        tool = "hold" if action.hold else "set_price"
        if not action.fallback and self.caps[s].grant(tool) is None:
            raise CapabilityError(f"{s} has no {tool} grant")
        last = self.state.last_price(s)
        fallback = action.fallback
        asked = None
        if action.hold and last is None:
            price, fallback = self.market.grid.snap(ref.nash_by_seller[s]), True
        elif action.hold:
            price = last.price
        else:
            price = self.market.grid.snap(action.price)
            if abs(price - action.price) > EPS:
                asked = action.price
        counts = dict(getattr(self.policies[s], "turn_stats", lambda _t: None)(t) or {})
        if queries.refused:
            counts["limits_hit"] = [*counts.get("limits_hit", []), self.spec.limits.queries(self.caps[s].max_queries_per_round)[1]]
        reason = action.fallback_reason if action.fallback else None
        self.rec.emit(
            ActedEvent, t, agent_id=s, price=price, hold=action.hold, fallback=fallback, fallback_reason=reason, asked_price=asked, **counts
        )
        if action.plan:
            self.rec.emit(PlannedEvent, t, agent_id=s, plan=action.plan)
        return price
