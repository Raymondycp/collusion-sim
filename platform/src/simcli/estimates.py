"""Run cost estimate and the server's defaults (contract 0.9.0). Both are reads: no model call, no paid gate, nothing stored."""

from __future__ import annotations

import click

from simcli.options import api, common, json_file
from simcli.output import col, emit, fields, table
from simcli.runs import runs

WINDOW_COLUMNS = [col("profile", "profile_id"), col("max_calls_per_5h"), col("used"), col("room"), col("calls_max"), col("enough")]
ESTIMATE_KEYS = (
    "call_mode",
    "n_runs",
    "calls_max",
    "tokens_max",
    "total_usd_max",
    "expected_cache_hits",
    "pricing_missing_for",
    "within_budget",
)
DEFAULT_COLUMNS = [col("role"), col("agent"), col("profile_id"), col("model")]


def estimate_text(e: dict) -> str:
    parts = [fields({k: e[k] for k in ESTIMATE_KEYS if k in e})]
    if e.get("rate_window"):
        parts.append(table(e["rate_window"], WINDOW_COLUMNS))
    parts.append("\n".join(f"warning: {w}" for w in e.get("warnings", [])))
    parts.append("\n".join(f"- {a}" for a in e.get("assumptions", [])))
    return "\n\n".join(p for p in parts if p)


@runs.command()
@click.option("--config", "config_file", required=True, help="JSON file with a RunConfig.")
@common
@click.pass_context
def estimate(ctx, config_file):
    """Worst-case calls, tokens and cost of a run config (POST /runs/estimate). No model call, no paid gate, stores nothing."""
    emit(ctx, api(ctx).post("runs/estimate", json_file(config_file, "config file")), estimate_text)


def default_rows(body: dict) -> list[dict]:
    rows = []
    for role, d in body.items():
        rows.append({"role": role, "agent": "", "profile_id": d.get("profile_id"), "model": d.get("model")})
        rows += [{"role": role, "agent": agent, **v} for agent, v in sorted(d.get("agents", {}).items())]
    return rows


@click.command()
@common
@click.pass_context
def defaults(ctx):
    """Default profile and model per role and agent (GET /defaults, from SIM_DEFAULT_*). They only pre-fill; research runs ignore them."""
    emit(ctx, api(ctx).get("defaults"), lambda body: table(default_rows(body), DEFAULT_COLUMNS))


COMMANDS = (defaults,)
