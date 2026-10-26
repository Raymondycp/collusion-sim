"""LLM agents in runs: profile binding (key and protocol checked at start, price frozen), one endpoint and one budget
guard per run, one 5-hour call window per profile, shared call cache.

Paid calls are refused unless the server runs with SIM_ALLOW_PAID=1. Tests pass a fake transport instead of a network.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from adapters.llm.budget import WINDOW_S, BudgetGuard, RateWindow
from adapters.llm.cache import CallCache
from adapters.llm.endpoint import Endpoint, ProviderProfile
from adapters.llm.policy import LLMPolicy, market_facts
from services.configs import AgentConfig, ApiError, BatchBudget, LlmFactory, RunConfig
from services.profiles import ProfileService, paid_calls_allowed
from simcore.run import RunSpec
from simcore.scenario import Scenario
from simcore.tools import TOOLS
from simschema.events import EventBase, RunLimits

History = Callable[[str, str], "tuple[list[str], int]"]
"""(profile id, since ISO) -> (wall times of paid calls, rate_limit errors). RunsDB.profile_calls."""


def _iso(epoch: float) -> str:
    """The recorder's wall_time format (utc_now), so a string compare in SQL is a time compare."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


PROMPTS = Path(__file__).parents[2] / "conditions" / "prompts.draft.yaml"


def default_prompt() -> str:
    """Exploratory default: the base wording v1 of the condition draft (no channel, no coordination text)."""
    return yaml.safe_load(PROMPTS.read_text(encoding="utf-8"))["base"]["v1"]


def prompt_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def uses_llm(config: RunConfig) -> bool:
    return any(a.policy.kind == "llm" for a in config.agents)


class LlmRuns:
    def __init__(
        self,
        profiles: ProfileService,
        cache: CallCache,
        transport: Callable[[], Any] | None = None,
        history: History | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.profiles, self.cache, self.transport, self.history, self.clock = profiles, cache, transport, history, clock
        self._windows: dict[str, RateWindow] = {}
        self._windows_lock = threading.Lock()

    def close(self) -> None:
        self.cache.close()

    def check_allowed(self, config: RunConfig) -> None:
        if uses_llm(config) and self.transport is None and not paid_calls_allowed(self.profiles.keys.env_file):
            raise ApiError(
                409,
                "paid_calls_disabled",
                "LLM agents make paid calls. The server runs with paid calls off (set SIM_ALLOW_PAID=1 after lead approval).",
            )

    def prompt(self, a: AgentConfig) -> str:
        return a.policy.llm.system_prompt or default_prompt()

    def prompt_hash(self, a: AgentConfig) -> str:
        """Covers the talk prompt too when the agent has one."""
        talk = a.policy.llm.talk_prompt
        return prompt_hash(self.prompt(a) + (f"\n\n[talk]\n{talk}" if talk else ""))

    def resolve(self, config: RunConfig) -> RunConfig:
        """The config with each LLM agent's prompt written out, so a later edit of the prompt file cannot change a stored run."""

        def one(a: AgentConfig) -> AgentConfig:
            if a.policy.kind != "llm" or a.policy.llm.system_prompt:
                return a
            llm = a.policy.llm.model_copy(update={"system_prompt": default_prompt()})
            return a.model_copy(update={"policy": a.policy.model_copy(update={"llm": llm})})

        return config.model_copy(update={"agents": [one(a) for a in config.agents]})

    def check_budget(self, config: RunConfig, batch: BatchBudget | BudgetGuard | None = None) -> None:
        """Q8: a run whose only cap is max_usd needs a price for every LLM agent, or no cap could ever stop it. A call or
        token cap of the shared batch (`config.batch_budget`, or an experiment's guard) is a cap that can act too."""
        caps = [config.budget, batch or config.batch_budget]
        if not uses_llm(config) or any(c is not None and (c.max_calls is not None or c.max_tokens is not None) for c in caps):
            return
        unpriced = sorted(
            {
                a.policy.llm.provider_profile_id
                for a in config.agents
                if a.policy.kind == "llm" and self.profiles.price_of(a.policy.llm.provider_profile_id, a.policy.llm.model) is None
            }
        )
        if unpriced:
            raise ApiError(
                422,
                "budget_unenforceable",
                f"Profiles {unpriced} have no price, so max_usd cannot stop their calls. Set max_calls or max_tokens too.",
                [{"path": "budget/max_calls", "message": "required when a profile has no price"}],
            )

    def bind(self, a: AgentConfig) -> dict:
        llm = a.policy.llm
        prof = self.profiles.bind(llm.provider_profile_id, llm.model, llm.model_family)
        return {
            "provider_profile_id": prof.id,
            "base_url_host": prof.host,
            "protocol": prof.protocol,
            "base_path": prof.base_path,
            "model": prof.model,
            "temperature": llm.temperature,
            "prompt_hash": self.prompt_hash(a),
            "price": prof.price() | {"as_of": _iso(self.clock())},
        }

    def budget(self, config: RunConfig, prior: list[EventBase], batch: BudgetGuard | None = None) -> BudgetGuard:
        """One guard per run. On resume it starts from what the run already spent, so a restart cannot double the cap."""
        b = config.budget
        guard = BudgetGuard(max_usd=b.max_usd, max_calls=b.max_calls, max_tokens=b.max_tokens, batch=batch)
        paid = [e for e in prior if e.type == "llm_call" and (not e.cache_hit or e.charged or (e.cost_usd or 0.0) > 0)]
        guard.calls = len(paid)
        guard.restore(  # the batch guard already holds this spend
            sum(e.cost_usd or 0.0 for e in paid),
            sum((e.input_tokens or 0) + (e.output_tokens or 0) for e in paid),
            sum(e.cost_usd is None for e in paid),
        )
        return guard

    def window(self, prof: ProviderProfile) -> RateWindow | None:
        """The profile's 5-hour window, shared by every run of this server; seeded from the stored calls of this data root."""
        if prof.max_calls_per_5h is None:
            return None
        with self._windows_lock:
            w = self._windows.get(prof.id)
            if w is None:
                stamps = self.history(prof.id, _iso(self.clock() - WINDOW_S))[0] if self.history else []
                w = self._windows[prof.id] = RateWindow(prof.max_calls_per_5h, [_epoch(s) for s in stamps], clock=self.clock)
            w.limit = prof.max_calls_per_5h  # one window per profile: a new limit keeps the calls in flight and every holder
            return w

    def window_status(self, profile_id: str) -> dict | None:
        """{max_calls_per_5h, used, room, rate_limited} of a profile, or None without a window. Per machine (this data root)."""
        try:
            rec = self.profiles.record(profile_id)
        except ApiError:
            return None
        if rec.max_calls_per_5h is None:
            return None
        stamps, limited = self.history(profile_id, _iso(self.clock() - WINDOW_S)) if self.history else ([], 0)
        with self._windows_lock:
            live = self._windows.get(profile_id)
        used = live.used() if live is not None else len(stamps)  # the live window counts calls not yet stored
        return {
            "max_calls_per_5h": rec.max_calls_per_5h,
            "used": used,
            "room": max(0, rec.max_calls_per_5h - used),
            "rate_limited": limited,
        }

    def factory(
        self, config: RunConfig, spec: RunSpec, budget: BudgetGuard, sample_index: int = 0, scenario: Scenario | None = None
    ) -> LlmFactory:
        """`scenario`: its base params give each seller's unit cost at round 1 and its market the price grid, stated in the
        prompt (DECISIONS-22 entry 5.1)."""
        base = scenario.market.demand.spec.parse(scenario.market.demand.params) if scenario else None
        grid = scenario.market.build().grid if scenario else None

        def make(a: AgentConfig, header, recorder) -> LLMPolicy:
            llm = a.policy.llm
            prof = self.profiles.bind(llm.provider_profile_id, llm.model, llm.model_family)
            key = self.profiles.keys.get(prof.id)
            if key is None:
                raise ApiError(422, "profile_key_missing", f"Provider profile {prof.id} has no key.")
            endpoint = Endpoint(
                prof,
                key,
                transport=self.transport() if self.transport else None,
                timeout_s=llm.timeout_s,
                session=f"{spec.run_id}:{a.id}",
            )
            limits = config.limits or RunLimits()
            grant = header.capability.grant("post")
            max_messages = limits.messages(TOOLS.params(grant).max_per_round if grant else 0)
            return LLMPolicy(
                a.id,
                endpoint,
                self.prompt(a),
                header.capability,
                recorder,
                self.cache,
                budget,
                temperature=llm.temperature,
                top_p=llm.top_p,
                max_output_tokens=llm.max_output_tokens,
                max_steps=limits.max_steps_per_turn,
                max_messages=max_messages,
                window=self.window(prof),
                run_seed=spec.seed,
                sample_index=sample_index,
                memory_every=config.memory.summary_every,
                memory_summary=config.memory.summary,
                talk_prompt=llm.talk_prompt,
                thinking=llm.thinking,
                thinking_on_timeout=llm.thinking_on_timeout,
                facts=market_facts(base.cost_of(a.id), grid, config.opening_price) if base is not None else None,
            )

        return make
