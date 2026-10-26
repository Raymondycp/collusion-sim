"""Scripted policies with known behaviour. They give known-answer tests with no LLM call."""

from __future__ import annotations

from typing import Any, Callable

from simcore.agents import Action, PolicyContext, Post
from simcore.view import ToolBox
from simschema.events import Observation


class Scripted:
    kind = "scripted"

    def __init__(self, ctx: PolicyContext) -> None:
        self.ctx = ctx
        self.state: dict[str, Any] = {}

    def talk(self, obs: Observation, tools: ToolBox) -> list[Post]:
        return []

    def act(self, obs: Observation, tools: ToolBox) -> Action:
        raise NotImplementedError

    def turn(self, obs: Observation, tools: ToolBox) -> tuple[list[Post], Action]:
        return self.talk(obs, tools), self.act(obs, tools)

    def after_round(self, t: int) -> tuple[str, int] | None:
        return None

    def dump(self) -> dict:
        return dict(self.state)

    def load(self, state: dict) -> None:
        self.state = dict(state)

    def nash(self, t: int) -> float:
        p = self.ctx.params(t)
        return self.ctx.market.grid.snap(self.ctx.market.demand.nash(self.ctx.sellers, p)[self.ctx.seller_id])

    def price(self, value: float) -> Action:
        return Action(price=self.ctx.market.grid.snap(value))

    def last_rival_prices(self, obs: Observation, tools: ToolBox) -> dict[str, float]:
        if tools.query_limit == 0:  # no queries (DECISIONS-13 §1): the push part holds the same prices
            return dict(obs.rival_last_prices or {})
        if not tools.has("get_rival_prices"):
            return {}
        rows = tools.call("get_rival_prices", rounds=1) or {}
        return {r: pts[-1]["price"] for r, pts in rows.items() if pts}


class ConstantPrice(Scripted):
    kind = "constant"

    def __init__(self, ctx: PolicyContext, price: float) -> None:
        super().__init__(ctx)
        self.value = price

    def act(self, obs, tools):
        return self.price(self.value)


class PriceSchedule(Scripted):
    kind = "price_schedule"

    def __init__(self, ctx: PolicyContext, prices: dict[int, float], default: float) -> None:
        super().__init__(ctx)
        self.prices = {int(k): v for k, v in prices.items()}
        self.default = default

    def act(self, obs, tools):
        return self.price(self.prices.get(obs.t, self.default))


class BestResponder(Scripted):
    """Best response to the rivals' last prices. Without rival data it prices at Nash."""

    kind = "best_response"

    def act(self, obs, tools):
        rivals = self.last_rival_prices(obs, tools)
        if not rivals:
            return self.price(self.nash(obs.t))
        return Action(price=self.ctx.market.best_response(self.ctx.seller_id, rivals, self.ctx.params(obs.t)))


class GrimTrigger(Scripted):
    """Holds `target`. If a rival priced below target − tolerance, prices at Nash for `punish_rounds` (None = forever)."""

    kind = "grim_trigger"

    def __init__(self, ctx: PolicyContext, target: float, punish_rounds: int | None = 3, tolerance: float = 0.01) -> None:
        super().__init__(ctx)
        self.target, self.punish_rounds, self.tolerance = target, punish_rounds, tolerance
        self.state = {"punish_until": None}

    def act(self, obs, tools):
        until = self.state["punish_until"]
        if until is not None and obs.t <= until:
            return self.price(self.nash(obs.t))
        if any(p < self.target - self.tolerance for p in self.last_rival_prices(obs, tools).values()):
            self.state["punish_until"] = 10**9 if self.punish_rounds is None else obs.t + self.punish_rounds - 1
            return self.price(self.nash(obs.t))
        return self.price(self.target)


class TitForTat(Scripted):
    kind = "tit_for_tat"

    def __init__(self, ctx: PolicyContext, start: float) -> None:
        super().__init__(ctx)
        self.start = start

    def act(self, obs, tools):
        rivals = self.last_rival_prices(obs, tools)
        return self.price(sum(rivals.values()) / len(rivals) if rivals else self.start)


class ScriptedTalker(Scripted):
    """Posts the same offer every round and never changes its price: talk with no effect on action."""

    kind = "scripted_talker"

    def __init__(self, ctx: PolicyContext, price: float, offer_text: str, channel_id: str) -> None:
        super().__init__(ctx)
        self.value, self.offer_text, self.channel_id = price, offer_text, channel_id

    def talk(self, obs, tools):
        return [Post(channel_id=self.channel_id, text=self.offer_text)]

    def act(self, obs, tools):
        return self.price(self.value)


SCRIPTED: dict[str, Callable[..., Scripted]] = {
    cls.kind: cls for cls in (ConstantPrice, PriceSchedule, BestResponder, GrimTrigger, TitForTat, ScriptedTalker)
}


def make_scripted(kind: str, ctx: PolicyContext, **params: Any) -> Scripted:
    return SCRIPTED[kind](ctx, **params)
