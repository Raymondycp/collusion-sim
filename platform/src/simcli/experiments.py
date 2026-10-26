"""Experiments: conditions × seeds cells, each a research parent run plus its planned branch children (contract 0.8.0)."""

from __future__ import annotations

import click

from simcli.client import CliError, seg
from simcli.options import api, common, json_file, paid, require_paid, writes
from simcli.output import col, emit, fields, table
from simcli.runs import Waiter, archive_command, max_wait_option, save, writable

EXPERIMENT_COLUMNS = [
    col("id"),
    col("name"),
    col("status"),
    col("split"),
    col("conditions"),
    col("seeds"),
    col("counts"),
    ("calls", lambda e: e["spend"]["calls"]),
    ("usd", lambda e: e["spend"]["usd"]),
    col("created_at"),
]
CELL_COLUMNS = [
    col("condition", "condition_id"),
    col("seed"),
    col("status"),
    ("parent", lambda c: c["parent"]["run_id"]),
    ("children", lambda c: len(c["branches"])),
    ("children_ended", lambda c: sum(b["status"] in ("completed", "stopped", "failed") for b in c["branches"])),
    ("error", lambda c: (c.get("error") or {}).get("code")),
]
LIVE = "running"
BLOCKED = "blocked"  # no cell is live and one cannot start its next run; the server retries only at its next start (CR-16 m1)
RESTART = "Fix the cause, then restart the server (`serve`); the server retries blocked cells at start."


def cell_error(c: dict) -> str:
    return f"{c['condition_id']} seed {c['seed']}: {c['error']['code']}: {c['error']['message']}"


def llm_agents(request) -> list[str]:
    """Agent ids with policy kind llm, in `agents` and in every `agents_by_condition` list."""
    if not isinstance(request, dict):
        return []
    lists = [("", request.get("agents") or [])]
    lists += [(f"{cid}/", agents or []) for cid, agents in (request.get("agents_by_condition") or {}).items()]
    return [f"{prefix}{a.get('id')}" for prefix, agents in lists for a in agents if (a.get("policy") or {}).get("kind") == "llm"]


def blocked(e: dict) -> CliError:
    """--wait on a blocked experiment: it makes no progress until the cause changes, as for a paused run (run_not_live)."""
    errors = [{"path": f"cells/{i}", "message": cell_error(c)} for i, c in enumerate(e["cells"]) if c.get("error")]
    message = f"Experiment {e['id']} is blocked: no cell can start its next run. {RESTART}"
    return CliError("run_not_live", message, errors=errors)


def experiment_text(e: dict) -> str:
    head = {k: e[k] for k in ("id", "name", "status", "split", "conditions", "seeds", "counts", "spend", "batch_budget") if k in e}
    estimate = e.get("estimate") or {}
    head |= {f"estimate.{k}": estimate[k] for k in ("calls_max", "usd_max", "within_budget") if k in estimate}
    text = f"{fields(head)}\n\n{table(e.get('cells', []), CELL_COLUMNS)}"
    errors = [cell_error(c) for c in e.get("cells", []) if c.get("error")]
    return text + (f"\n\nblocked cells. {RESTART}\n" + "\n".join(errors) if errors else "")


def estimate_text(e: dict) -> str:
    head = {k: e[k] for k in ("n_cells", "runs", "calls_max", "tokens_max", "usd_max", "within_budget", "pricing_missing_for") if k in e}
    per = table(e.get("per_condition", []), [col("condition", "condition_id"), col("runs"), col("calls_max"), col("usd_max")])
    notes = "\n".join(f"- {a}" for a in e.get("assumptions", []))
    return "\n\n".join(part for part in (fields(head), per, notes) if part)


@click.group()
def experiments():
    """Experiments: conditions × seeds cells with planned branch children (research runs only)."""


@experiments.command("list")
@click.option("--include-archived", is_flag=True, help="Also list archived experiments.")
@common
@click.pass_context
def list_experiments(ctx, include_archived):
    """All experiments, newest first; archived ones only with --include-archived (GET /experiments)."""
    params = {"include_archived": "true"} if include_archived else {}
    emit(ctx, api(ctx).get("experiments", **params), lambda rows: table(rows, EXPERIMENT_COLUMNS))


for _action in ("archive", "unarchive"):
    archive_command(experiments, "experiments", _action)


@experiments.command()
@click.argument("experiment_id")
@common
@click.pass_context
def show(ctx, experiment_id):
    """Progress per cell, spend against the batch budget and the stored estimate (GET /experiments/{id})."""
    emit(ctx, api(ctx).get(f"experiments/{seg(experiment_id)}"), experiment_text)


@experiments.command()
@click.argument("experiment_id")
@click.option("-o", "--output", required=True, type=click.Path(dir_okay=False), help="Zip file to write.")
@click.option("--overwrite", is_flag=True, help="Replace an existing file.")
@common
@click.pass_context
def export(ctx, experiment_id, output, overwrite):
    """Write the experiment and its runs to a zip (GET /experiments/{id}/export).

    The zip holds experiment.json, runs/<run_id>/ with episode.jsonl, meta.json and trail.jsonl, and manifest.json."""
    target = writable(output, overwrite)
    data = api(ctx).raw(f"experiments/{seg(experiment_id)}/export", format="zip")
    emit(ctx, {**save(target, data), "format": "zip", "experiment_id": experiment_id}, fields)


@experiments.command()
@click.option("--file", "request_file", required=True, help="JSON file with an ExperimentCreate request.")
@common
@click.pass_context
def estimate(ctx, request_file):
    """Worst-case calls, tokens and USD (POST /experiments/estimate). Every start check but the paid gate; no LLM call, stores nothing."""
    emit(ctx, api(ctx).post("experiments/estimate", json_file(request_file, "experiment file")), estimate_text)


@experiments.command()
@click.option("--file", "request_file", required=True, help="JSON file with an ExperimentCreate request.")
@click.option("--wait", is_flag=True, help="Poll until the experiment is completed or ended; print it.")
@max_wait_option
@paid
@writes
@common
@click.pass_context
def start(ctx, request_file, wait, max_wait, allow_paid):
    """Start the experiment (POST /experiments). Research fields come from the conditions; the server checks them."""
    request = json_file(request_file, "experiment file")
    llm = llm_agents(request)
    if llm:
        require_paid(ctx, allow_paid, f"Agents {', '.join(llm)} have policy kind llm.")
    started = api(ctx).post("experiments", request)
    if not wait:
        return emit(ctx, started, experiment_text)
    waiter, eid = Waiter(ctx, max_wait), started["id"]
    while True:
        body = api(ctx).get(f"experiments/{seg(eid)}")
        if body["status"] == BLOCKED:
            raise blocked(body)
        if body["status"] != LIVE:
            return emit(ctx, body, experiment_text)
        for cell in body["cells"]:
            for run in (cell["parent"], *cell["branches"]):
                waiter.check(run["run_id"], run["status"])
        waiter.pause()


COMMANDS = (experiments,)
