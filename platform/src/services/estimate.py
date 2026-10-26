"""Worst-case calls, tokens and USD of runs, before they start (POST /runs/estimate, POST /experiments/estimate). No LLM call.

Works on bound per-seed configs: each agent has its capability and its prompts written out, and call_mode is set.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable

from adapters.llm.policy import ATTEMPTS, OPEN_OUTPUT_TOKENS
from services.configs import RunConfig, call_plan_of
from simcore.tools import TOOLS, PostInput
from simschema.events import Capability, RunLimits

RENDER_CHARS = 800
"""Upper estimate of the rendered per-round user message (round, own results, message count, headlines, instruction)."""
TOOL_RESULT_TOKENS = 600
"""Upper estimate of the tool results that one tool step adds to the next request."""
SUMMARY_CHARS = 1200
"""Upper estimate of a memory-summary request besides the system prompt (three earlier notes, the facts, the instruction)."""
LINE_CHARS = 80
"""Upper estimate of one rendered line of the record: an own round, a rival price, or a message's header."""
NEWS_CHARS = 400
"""Upper estimate of the news items of one round."""
POST_CHARS = PostInput.model_fields["text"].metadata[0].max_length

Usage = dict[tuple[str, str], list[int]]
"""(provider profile id, model) -> [calls, input tokens, output tokens]."""

PriceOf = Callable[[str, str], "tuple[float, float] | None"]
"""(profile id, model) -> (input, output) USD per million tokens, or None without a price."""


def add(into: Usage, key: tuple[str, str], calls: int, inp: int, out: int, times: int = 1) -> None:
    row = into.setdefault(key, [0, 0, 0])
    row[0], row[1], row[2] = row[0] + calls * times, row[1] + inp * times, row[2] + out * times


def _tool_chars(grants) -> int:
    specs = {s.id: s for s in TOOLS.specs()}
    return sum(
        len(json.dumps({"name": g.tool_id, "description": specs[g.tool_id].description, "parameters": specs[g.tool_id].input_schema}))
        for g in grants
    )


def _turn(system_chars: int, tool_chars: int, max_out: int, steps: int) -> tuple[int, int, int]:
    """(calls, input tokens, output tokens) of one turn at worst: `steps` tool steps, each with ATTEMPTS attempts."""
    base = math.ceil((system_chars + RENDER_CHARS + tool_chars) / 3)
    inp = sum(base + k * (max_out + TOOL_RESULT_TOKENS) for k in range(steps)) * ATTEMPTS
    calls = steps * ATTEMPTS
    return calls, inp, calls * max_out


def turns(config: RunConfig) -> dict[str, int]:
    """LLM turns of one round by phase (talk, act, one_call)."""
    out = {"talk": 0, "act": 0, "one_call": 0}
    plan = call_plan_of(config)
    for a in config.agents:
        if a.role == "seller" and a.policy.kind == "llm":
            for purpose in plan.sellers[a.id].calls:
                out[purpose] += 1
    return out


def history_chars(config: RunConfig) -> int:
    """Worst-case characters that one round adds to every later user message: each call shows the whole record so far
    (DECISIONS-15 §1), with every post at its maximum length."""
    limits = config.limits or RunLimits()
    sellers = [a for a in config.agents if a.role == "seller"]
    posters = sum(1 for a in sellers if (a.capability or Capability()).grant("post") is not None)
    return LINE_CHARS * len(sellers) + NEWS_CHARS + posters * limits.max_messages_per_round * (POST_CHARS + LINE_CHARS)


def round_usage(config: RunConfig, record_chars: int = 0) -> Usage:
    """Worst-case calls and tokens of one round, per (profile, model). `record_chars`: the earlier rounds' record."""
    limits = config.limits or RunLimits()
    plan = call_plan_of(config)
    out: Usage = {}
    for a in config.agents:
        if a.role != "seller" or a.policy.kind != "llm":
            continue
        llm, cap = a.policy.llm, a.capability or Capability()
        # The call plan's calls (CR-78 I13): a talk call only with a postable channel
        prompts = {"talk": llm.talk_prompt or llm.system_prompt or ""}
        phases = [(p, prompts.get(p, llm.system_prompt or "")) for p in plan.sellers[a.id].calls]
        for phase, system in phases:
            steps = plan.sellers[a.id].steps(phase, limits.max_steps_per_turn)
            turn = _turn(len(system) + record_chars, _tool_chars(cap.grants), llm.max_output_tokens or OPEN_OUTPUT_TOKENS, steps)
            add(out, (llm.provider_profile_id, llm.model), *turn)
    return out


def summary_usage(config: RunConfig) -> Usage:
    """Worst-case calls and tokens of one LLM memory summary per LLM seller: one request, ATTEMPTS attempts, no tools."""
    out: Usage = {}
    for a in config.agents:
        if a.role == "seller" and a.policy.kind == "llm":
            llm = a.policy.llm
            inp = math.ceil((len(llm.system_prompt or "") + SUMMARY_CHARS) / 3)
            add(
                out,
                (llm.provider_profile_id, llm.model),
                ATTEMPTS,
                inp * ATTEMPTS,
                (llm.max_output_tokens or OPEN_OUTPUT_TOKENS) * ATTEMPTS,
            )
    return out


def run_usage(config: RunConfig, rounds: int, first: int = 1) -> Usage:
    """Worst-case usage of one run of `rounds` rounds from round `first` (a branch child starts at its branch round)."""
    out: Usage = {}
    per_round = history_chars(config)
    for t in range(first, first + rounds):
        for key, row in round_usage(config, (t - 1) * per_round).items():
            add(out, key, *row)
    if config.memory.summary == "llm":
        for key, row in summary_usage(config).items():
            add(out, key, *row, times=rounds // config.memory.summary_every)
    return out


def usd(usage: Usage, price_of: PriceOf) -> tuple[float | None, set[str]]:
    """USD of a usage, or None when a profile has no price; and the profile ids with no price."""
    total: float | None = 0.0
    missing = set()
    for (pid, model), (_, inp, out) in usage.items():
        price = price_of(pid, model)
        if price is None:
            missing.add(pid)
            total = None
        elif total is not None:
            total += (price[0] * inp + price[1] * out) / 1e6
    return (None if total is None else round(total, 6)), missing


def totals(usage: Usage) -> tuple[int, int, int]:
    return tuple(sum(r[i] for r in usage.values()) for i in range(3))  # type: ignore[return-value]


def assumptions(steps: str) -> list[str]:
    return [
        f"Every LLM turn uses all {steps} tool steps (limits.max_steps_per_turn) and every step all {ATTEMPTS} attempts; "
        "nothing comes from the call cache. With max_queries_per_round 0 an act turn, and a talk turn with one channel, has"
        " one step (DECISIONS-13 §1).",
        f"Input per call: (system prompt + {RENDER_CHARS} message characters + the record + tool schemas) / 3 tokens, plus "
        f"{TOOL_RESULT_TOKENS} tokens of tool results and max_output_tokens per earlier step. A null max_output_tokens (no "
        f"limit) counts as {OPEN_OUTPUT_TOKENS} output tokens per call.",
        f"The record (DECISIONS-15 §1) grows each earlier round by {LINE_CHARS} characters per seller, {NEWS_CHARS} of news "
        f"and, per seller with a post grant, max_messages_per_round posts of {POST_CHARS} characters plus {LINE_CHARS}.",
        f"With memory.summary llm: one summary request per LLM seller every summary_every rounds, {ATTEMPTS} attempts, "
        f"input (system prompt + {SUMMARY_CHARS} characters) / 3 tokens.",
        "Each run lasts its seed's stop round; branch children last their planned rounds. Scripted sellers make no calls.",
        "Prices per provider profile and model (model_pricing, then the profile price). Cached input is priced as input.",
    ]


def window_rows(per_profile: dict[str, int], window_of: Callable[[str], dict | None]) -> tuple[list[dict], list[str]]:
    """m5: compare each profile's worst-case calls with the room left in its 5-hour window, before the start."""
    rows, warnings = [], []
    for pid, n in sorted(per_profile.items()):
        w = window_of(pid)
        if w is not None:
            rows.append({"profile_id": pid, **w, "calls_max": n, "enough": n <= w["room"]})
            if n > w["room"]:
                warnings.append(
                    f"Profile {pid}: up to {n} calls, but only {w['room']} of {w['max_calls_per_5h']} are left in its 5-hour window "
                    "(counted on this machine). A full window stops the run with end reason rate_window."
                )
    return rows, warnings


def runs_estimate(units: list[tuple[RunConfig, int]], price_of: PriceOf, window_of: Callable[[str], dict | None]) -> dict:
    """POST /runs/estimate. `units`: (bound one-seed config, its stop round) per run. `per_run` is the most expensive run."""
    worst: dict | None = None
    calls_all, inp_all, out_all = 0, 0, 0
    usd_all: float | None = 0.0
    missing: set[str] = set()
    per_profile: dict[str, int] = {}
    budget = units[0][0].budget
    checks: list[bool | None] = []
    for cfg, rounds in units:
        usage = run_usage(cfg, rounds)
        calls, inp, out = totals(usage)
        usd_run, gaps = usd(usage, price_of)
        missing |= gaps
        for (pid, _), row in usage.items():
            per_profile[pid] = per_profile.get(pid, 0) + row[0]
        steps = (cfg.limits or RunLimits()).max_steps_per_turn
        per_turn = steps * ATTEMPTS
        by_phase = {k: n * per_turn * rounds for k, n in turns(cfg).items()}
        summary = calls - sum(by_phase.values())
        run = {
            "max_rounds": rounds,
            "calls": by_phase | {"summary": summary, "total_max": calls},
            "tokens_max": {"input": inp, "output": out},
            "usd_max": usd_run,
        }
        if worst is None or calls > worst["calls"]["total_max"]:
            worst = run
        calls_all, inp_all, out_all = calls_all + calls, inp_all + inp, out_all + out
        usd_all = None if usd_all is None or usd_run is None else round(usd_all + usd_run, 6)
        if budget.max_calls is not None:
            checks.append(calls <= budget.max_calls)
        if budget.max_tokens is not None:
            checks.append(inp + out <= budget.max_tokens)
        if budget.max_usd is not None:
            checks.append(None if usd_run is None else usd_run <= budget.max_usd)
    windows, warnings = window_rows(per_profile, window_of)
    if missing and budget.max_calls is None and budget.max_tokens is None:
        warnings.append(
            f"Profiles {sorted(missing)} have no price and the budget has only max_usd: the start is refused (budget_unenforceable)."
        )
    return {
        "call_mode": units[0][0].call_mode,
        "n_runs": len(units),
        "per_run": worst,
        "calls_max": calls_all,
        "tokens_max": {"input": inp_all, "output": out_all},
        "total_usd_max": usd_all,
        "expected_cache_hits": 0,
        "pricing_missing_for": sorted(missing),
        "within_budget": None if None in checks else all(checks),
        "rate_window": windows,
        "warnings": warnings,
        "assumptions": assumptions(str(max((c.limits or RunLimits()).max_steps_per_turn for c, _ in units))),
    }
