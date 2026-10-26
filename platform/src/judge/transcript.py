"""The transcript a judge scores: the blinded view of a run, with copies of the run's own prompts removed (plan A7). In
features_code_hash with the other files that shape scores; the service that reads the run from the store is not."""

from __future__ import annotations

from judge.views import Rendered, TransformId, ViewId, render
from services.configs import RunConfig
from services.llm_runs import default_prompt
from simschema.events import EventBase


def echo_prompts(config: RunConfig) -> list[str]:
    """Every system and talk prompt the run's sellers got: the echo mask removes copies of them."""
    out = []
    for a in config.agents:
        llm = a.policy.llm
        if llm is not None:
            out += [p for p in (llm.system_prompt or default_prompt(), llm.talk_prompt) if p]
    return out


def run_view(
    view: ViewId, run_id: str, events: list[EventBase], config: RunConfig, transform: TransformId, trail: list[dict] | None = None
) -> Rendered:
    return render(view, run_id, events, echo_prompts(config), transform, trail)
