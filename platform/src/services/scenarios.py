"""Scenario read models that are pure calculation: the reference series for the preview chart and the demand helper."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from services.catalog import parse_draft
from services.configs import ApiError
from simcore.demand import DEMAND
from simcore.generators import GENERATORS
from simcore.market import Market, PriceGrid
from simcore.scenario import Scenario


def reference_series(scenario: Scenario, until: int | None) -> dict:
    """params(t), costs, Nash and joint optimum per round, and event markers, for rounds 1..until (default max_rounds).

    A round whose shocks give invalid params is left out; the scenario check names the problem.
    """
    spec = scenario.market.demand.spec
    last = min(until or scenario.horizon.max_rounds, scenario.horizon.max_rounds)
    market = scenario.market.build()
    rounds = []
    for t in range(1, last + 1):
        try:
            p = scenario.params(t)
        except ValueError:
            continue
        point: dict[str, Any] = {"t": t, "params": p.model_dump(mode="json"), "costs": {s: p.cost_of(s) for s in scenario.seller_ids}}
        if spec.research_allowed:
            point |= market.reference(p, scenario.seller_ids).model_dump()
        rounds.append(point)
    markers = []
    for i, e in enumerate(scenario.events):
        markers.append({"t": e.announce, "kind": "announce", "event_index": i})
        if e.shocks:
            markers += [{"t": e.effect, "kind": "effect", "event_index": i}, {"t": e.effect + e.length, "kind": "end", "event_index": i}]
    return {
        "scenario_id": scenario.id,
        "hash": scenario.hash,
        "demand_model": spec.id,
        "demand_version": spec.version,
        "references_available": spec.research_allowed,
        "rounds": rounds,
        "markers": sorted((m for m in markers if m["t"] <= last), key=lambda m: m["t"]),
    }


def solve(model_id: str, targets: dict) -> dict:
    try:
        spec = DEMAND.get(model_id)
    except KeyError:
        raise ApiError(404, "not_found", f"Demand model {model_id} is not registered.") from None
    if spec.solver is None:
        raise ApiError(422, "solver_not_available", f"Demand model {model_id} has no target solver.")
    try:
        params = spec.solver.solve(spec.solver.input_model.model_validate(targets))
    except ValidationError as e:
        raise ApiError(
            422,
            "validation_error",
            "The targets do not fit the solver input.",
            [{"path": "/".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()],
        ) from None
    sellers = ("A", "B")
    ref = Market(spec.build(), PriceGrid()).reference(params, sellers)
    return {
        "model_id": spec.id,
        "version": spec.version,
        "params": params.model_dump(mode="json"),
        "nash": ref.nash,
        "joint_optimum": ref.joint_optimum,
        "problems": spec.checks(params, sellers),
    }


DRAFT_EXCLUDE = {"id", "status", "hash", "approved_by", "frozen_at"}


def generate(generator_id: str, inputs: dict, seed: int) -> dict:
    """Run a generator and its check. Saves nothing: the person saves the draft, checks it, approves and freezes it."""
    try:
        gen = GENERATORS.get(generator_id)
    except KeyError:
        raise ApiError(404, "unknown_generator", f"Scenario generator {generator_id} is not registered.") from None
    if gen.paid:
        raise ApiError(409, "conflict", f"Generator {generator_id} makes paid calls and needs lead approval with a budget cap.")
    try:
        draft = gen.generate(inputs, seed)
    except ValidationError as e:
        raise ApiError(
            422,
            "validation_error",
            "The inputs do not fit the generator.",
            [{"path": "inputs/" + "/".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()],
        ) from None
    except ValueError as e:
        raise ApiError(422, "validation_error", str(e)) from None
    return {"draft": draft.model_dump(mode="json", exclude=DRAFT_EXCLUDE), "check": draft.check().model_dump(mode="json"), "cost_usd": 0.0}


PREVIEW_CHECKS = {"shock_paths", "stability", "news_direction", "visible_ids"}


def market_preview(body: dict) -> dict:
    """Reference series for an unsaved market and event list. Horizon and quiet window are not part of a preview."""
    rounds = body.get("rounds")
    if not isinstance(rounds, int) or not 1 <= rounds <= 200:
        raise ApiError(422, "validation_error", "rounds must be an integer from 1 to 200.", [{"path": "rounds", "message": "1..200"}])
    draft = parse_draft(
        "preview",
        {
            "market": body.get("market"),
            "n_sellers": body.get("n_sellers", 2),
            "events": body.get("events", []),
            "name": "preview",
            "horizon": {"min_rounds": rounds, "max_rounds": rounds, "stop_prob": 0},
            "quiet_window": (0, 0),
        },
    )
    checks = [c.model_dump(mode="json") for c in draft.check().checks if c.id in PREVIEW_CHECKS]
    return {"rounds": reference_series(draft, rounds)["rounds"], "checks": checks}
