"""Runs, branches and indicators."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import click

from simcli.client import CliError, seg
from simcli.options import api, common, json_arg, json_file, paid, require_paid, writes
from simcli.output import cell, col, dumps, emit, fields, table

TERMINAL = {"completed", "stopped", "failed"}
NOT_LIVE = {"paused", "recovering"}
POLL_SECONDS = 1.0
REPO = Path(__file__).resolve().parents[2]
SERVER_FOLDERS = ("data", "cache", "runs", "scenarios")  # the folders the server owns under SIM_ROOT (services/catalog.py Paths)

RUN_COLUMNS = [
    col("id"),
    col("kind"),
    col("status"),
    col("condition", "condition_id"),
    col("split"),
    col("seed"),
    ("t/max", lambda r: f"{cell(r.get('t'))}/{cell(r.get('max_rounds'))}"),
    col("cost_usd"),
    col("calls"),
    col("created_at"),
]


def run_table(rows: list[dict]) -> str:
    return table(rows, RUN_COLUMNS)


class Waiter:
    """Polling for --follow and --wait: sleep between polls, give up after --max-wait (CLI-DESIGN §4.3)."""

    def __init__(self, ctx: click.Context, max_wait: float | None):
        obj = ctx.find_root().obj
        self.sleep, self.clock = obj.get("sleep", time.sleep), obj.get("clock", time.monotonic)
        self.deadline = None if max_wait is None else self.clock() + max_wait
        self.max_wait = max_wait

    def check(self, run_id: str, status: str) -> None:
        if status in NOT_LIVE:
            raise CliError("run_not_live", f"Run {run_id} is {status}: it makes no progress until it is resumed or recovered.")

    def pause(self) -> None:
        if self.deadline is not None and self.clock() >= self.deadline:
            raise CliError("wait_timeout", f"Stopped after --max-wait {self.max_wait:g} s; the run is still live.")
        self.sleep(POLL_SECONDS)


max_wait_option = click.option("--max-wait", type=float, help="Give up after this many seconds (wait_timeout). Default: no limit.")


@click.group()
def runs():
    """Runs: list, inspect, events, series, state at t, observations."""


@runs.command("list")
@click.option("--kind", type=click.Choice(["research", "exploratory"]))
@click.option("--status")
@click.option("--condition", "condition_id")
@click.option("--split")
@click.option("--scenario", "scenario_id")
@click.option("--batch", "batch_id")
@click.option("--parent", "parent_run_id")
@click.option("--preset", "preset_id")
@click.option("--model")
@click.option("--from", "created_from", help="ISO 8601 time.")
@click.option("--to", "created_to", help="ISO 8601 time.")
@click.option("--include-archived", is_flag=True, help="Also list archived runs.")
@click.option("--limit", type=int)
@click.option("--cursor")
@click.option("--all", "all_pages", is_flag=True, help="Follow next_cursor to the end.")
@common
@click.pass_context
def list_runs(ctx, all_pages, include_archived, **filters):
    """Runs from the ledger, newest first; archived runs only with --include-archived (GET /runs)."""
    filters |= {"include_archived": "true"} if include_archived else {}
    page = api(ctx).get("runs", **filters)
    items = page["items"]
    while all_pages and page.get("next_cursor"):
        page = api(ctx).get("runs", **{**filters, "cursor": page["next_cursor"]})
        items += page["items"]
    body = {"items": items, "next_cursor": None} if all_pages else page
    emit(ctx, body, lambda b: run_table(b["items"]) + (f"\nnext cursor: {b['next_cursor']}" if b.get("next_cursor") else ""))


@runs.command()
@click.argument("run_id")
@common
@click.pass_context
def show(ctx, run_id):
    """One run with its config, snapshots and children (GET /runs/{id})."""
    emit(ctx, api(ctx).get(f"runs/{seg(run_id)}"))


@runs.command()
@click.option("--config", "config_file", required=True, help="JSON file with a RunConfig (one run per seed).")
@click.option("--wait", is_flag=True, help="Poll until every run is completed, stopped or failed; print the final summaries.")
@max_wait_option
@paid
@writes
@common
@click.pass_context
def start(ctx, config_file, wait, max_wait, allow_paid):
    """Start one run per seed (POST /runs). Research fields come from the condition; the server checks them."""
    config = json_file(config_file, "config file")
    agents = config.get("agents", []) if isinstance(config, dict) else []
    llm = [a.get("id") for a in agents if (a.get("policy") or {}).get("kind") == "llm"]
    if llm:
        require_paid(ctx, allow_paid, f"Agents {', '.join(map(str, llm))} have policy kind llm.")
    started = api(ctx).post("runs", config)
    if not wait:
        return emit(ctx, started, fields)
    waiter, ids = Waiter(ctx, max_wait), set(started["run_ids"])
    while True:
        page = api(ctx).get("runs", batch_id=started["batch_id"], limit=max(len(ids), 1))
        mine = [r for r in page["items"] if r["id"] in ids]
        if len(mine) == len(ids) and all(r["status"] in TERMINAL for r in mine):
            body = {**started, "runs": mine}
            return emit(ctx, body, lambda b: f"batch_id  {b['batch_id']}\n" + run_table(b["runs"]))
        for r in mine:
            waiter.check(r["id"], r["status"])
        waiter.pause()


def control(action: str):
    @runs.command(action)
    @click.argument("run_id")
    @writes
    @common
    @click.pass_context
    def command(ctx, run_id):
        emit(ctx, api(ctx).post(f"runs/{seg(run_id)}/{action}"))

    command.help = f"POST /runs/{{id}}/{action}. An ended run gives run_not_active."
    return command


for _action in ("pause", "resume", "stop"):
    control(_action)


def archive_command(group: click.Group, plural: str, action: str) -> None:
    """`<group> archive|unarchive [IDS]... | --all` (DECISIONS-29): POST /<plural>/<action> with ids or all."""

    @group.command(action)
    @click.argument("ids", nargs=-1)
    @click.option(
        "--all",
        "every",
        is_flag=True,
        help=f"Every {plural[:-1]} on the server that is {'not ' if action == 'archive' else ''}archived; list filters do not apply.",
    )
    @writes
    @common
    @click.pass_context
    def command(ctx, ids, every):
        if bool(ids) == every:
            raise CliError("cli_usage", "Give ids or --all." if not ids else "Give ids or --all, not both.", exit_code=2)
        emit(ctx, api(ctx).post(f"{plural}/{action}", {"all": True} if every else {"ids": list(ids)}))

    verb = "Archive" if action == "archive" else "Restore archived"
    live = f" A running {plural[:-1]} is refused (conflict); --all leaves it out (skipped)." if action == "archive" else ""
    effect = "hidden from the list, nothing deleted" if action == "archive" else "returned to the list"
    command.help = f"{verb} {plural}: {effect} (POST /{plural}/{action}).{live}"


for _action in ("archive", "unarchive"):
    archive_command(runs, "runs", _action)


def refused_dirs() -> list[Path]:
    roots = {REPO, Path.cwd().resolve(), *([Path(os.environ["SIM_ROOT"]).resolve()] if os.environ.get("SIM_ROOT") else [])}
    return [root / name for root in roots for name in SERVER_FOLDERS]


def writable(output: str, overwrite: bool) -> Path:
    """The export target, refused inside the server's folders and, without --overwrite, when it exists. Checked before any request."""
    target = Path(output).resolve()
    for refused in refused_dirs():
        if target.is_relative_to(refused):
            raise CliError("path_refused", f"{target} is inside {refused}: the server owns that folder.")
    if target.exists() and not overwrite:
        raise CliError("path_refused", f"{target} exists. Pass --overwrite to replace it.")
    return target


def save(target: Path, data: bytes) -> dict:
    """Write the bytes unchanged (through a .part file) and return {path, bytes, sha256}."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data)
    partial.replace(target)
    return {"path": str(target), "bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}


@runs.command()
@click.argument("run_id")
@click.option("--format", "fmt", type=click.Choice(["zip", "csv_rounds", "jsonl"]), default="zip", show_default=True)
@click.option("--include-branches", is_flag=True, help="zip only: add the branch children.")
@click.option("-o", "--output", required=True, type=click.Path(dir_okay=False), help="File to write.")
@click.option("--overwrite", is_flag=True, help="Replace an existing file.")
@common
@click.pass_context
def export(ctx, run_id, fmt, include_branches, output, overwrite):
    """Write the run record to a file (GET /runs/{id}/export, or /episode.jsonl for jsonl)."""
    if include_branches and fmt != "zip":
        raise click.UsageError("--include-branches needs --format zip.")
    target = writable(output, overwrite)
    if fmt == "jsonl":
        data = api(ctx).raw(f"runs/{seg(run_id)}/episode.jsonl")
    else:
        data = api(ctx).raw(f"runs/{seg(run_id)}/export", format=fmt, include="branches" if include_branches else None)
    emit(ctx, {**save(target, data), "format": fmt, "run_id": run_id}, fields)


def event_line(e: dict) -> str:
    detail = {
        "acted": lambda: "hold" if e.get("hold") else f"price={e.get('price')}",
        "posted": lambda: f"{e.get('channel_id')}: {e.get('text', '')[:60]}",
        "settled": lambda: f"prices={cell(e.get('prices'))}",
        "run_status_changed": lambda: str(e.get("status")),
        "run_ended": lambda: f"{e.get('status')} {e.get('end_reason')}",
        "tool_called": lambda: f"{e.get('tool')} allowed={e.get('allowed')}",
    }.get(e["type"], lambda: "")()
    return f"{e['seq']:>6}  t={cell(e.get('t')):<6} {e.get('tier', '')} {e['type']:<20} {cell(e.get('agent_id')):<5} {detail}".rstrip()


@runs.command()
@click.argument("run_id")
@click.option("--from-seq", type=int)
@click.option("--types", help="Comma-separated SimEvent types, for example posted,acted,settled.")
@click.option("--agent", "agent_id")
@click.option("--limit", type=int)
@click.option("--follow", is_flag=True, help="Poll until the run ends. With --json: JSON Lines, one SimEvent per line.")
@max_wait_option
@common
@click.pass_context
def events(ctx, run_id, from_seq, types, agent_id, limit, follow, max_wait):
    """Stored events in seq order (GET /runs/{id}/events)."""
    filters = {"types": types, "agent_id": agent_id, "limit": limit}
    if not follow:
        body = api(ctx).get(f"runs/{seg(run_id)}/events", from_seq=from_seq, **filters)
        more = f"\nnext from_seq: {body['next_from_seq']}" if body.get("next_from_seq") is not None else ""
        return emit(ctx, body, lambda b: "\n".join(event_line(e) for e in b["events"]) + more)
    as_json, waiter = ctx.find_root().obj["json"], Waiter(ctx, max_wait)
    seq, last_check = from_seq or 0, False
    while True:
        page = api(ctx).get(f"runs/{seg(run_id)}/events", from_seq=seq, **filters)
        for e in page["events"]:
            click.echo(dumps(e, indent=None) if as_json else event_line(e))
            if e["type"] == "run_ended":
                return
        if page.get("next_from_seq") is not None:
            seq = page["next_from_seq"]
            continue
        seq = max(seq, page["last_seq"] + 1)
        if last_check:
            return
        status = api(ctx).get(f"runs/{seg(run_id)}")["status"]
        last_check = status in TERMINAL
        if not last_check:
            waiter.check(run_id, status)
            waiter.pause()


@runs.command()
@click.argument("run_id")
@common
@click.pass_context
def series(ctx, run_id):
    """Chart data per round: prices, reference lines, surplus, markers (GET /runs/{id}/series)."""

    def human(b):
        lines = {f"price.{s}": v["price"] for s, v in sorted(b["sellers"].items())}
        lines |= {k: b["reference"].get(k) or [None] * len(b["t"]) for k in ("nash", "joint_optimum")}
        rows = [{"t": t, **{name: values[i] for name, values in lines.items()}} for i, t in enumerate(b["t"])]
        return table(rows, [col("t")] + [col(name) for name in lines])

    emit(ctx, api(ctx).get(f"runs/{seg(run_id)}/series"), human)


@runs.command()
@click.argument("run_id")
@click.option("--t", "t", type=int, required=True, help="Round.")
@common
@click.pass_context
def state(ctx, run_id, t):
    """State folded at the end of round t (GET /runs/{id}/state)."""
    emit(ctx, api(ctx).get(f"runs/{seg(run_id)}/state", t=t), fields)


@runs.command()
@click.argument("run_id")
@click.option("--agent", "agent_id", required=True)
@click.option("--t", "t", type=int, required=True, help="Round.")
@common
@click.pass_context
def observations(ctx, run_id, agent_id, t):
    """What one agent saw at round t, and the exact LLM requests (GET /runs/{id}/observations)."""
    emit(ctx, api(ctx).get(f"runs/{seg(run_id)}/observations", agent_id=agent_id, t=t), fields)


@click.group()
def branches():
    """Branch children of a run (probe, placebo, ablation)."""


@branches.command("list")
@click.argument("run_id")
@common
@click.pass_context
def list_branches(ctx, run_id):
    """Children of a run (GET /runs/{id}/branches)."""
    emit(ctx, api(ctx).get(f"runs/{seg(run_id)}/branches"), run_table)


@branches.command("create")
@click.argument("run_id")
@click.option("--file", "branch_file", required=True, help="JSON file with a BranchRequest: at, rounds, n, intervention, budget?.")
@paid
@writes
@common
@click.pass_context
def create_branches(ctx, run_id, branch_file, allow_paid):
    """Fork a run at t and start the children (POST /runs/{id}/branches)."""
    request = json_file(branch_file, "branch file")
    models = api(ctx).get(f"runs/{seg(run_id)}").get("models") or []
    if models:
        require_paid(ctx, allow_paid, f"Run {run_id} has LLM agents ({', '.join(models)}); its children call them again.")
    emit(ctx, api(ctx).post(f"runs/{seg(run_id)}/branches", request), fields)


@click.group()
def indicators():
    """Indicator registry and per-run indicator series."""


@indicators.command("list")
@common
@click.pass_context
def list_indicators(ctx):
    """The indicator registry (GET /indicators)."""
    cols = [col("id"), col("version"), col("data_layer"), col("pane"), ("outputs", lambda r: [o["name"] for o in r.get("outputs", [])])]
    emit(ctx, api(ctx).get("indicators"), lambda rows: table(rows, cols))


@indicators.command("series")
@click.argument("run_id")
@click.argument("indicator_id")
@click.option("--params", "params_json", help='Indicator params as JSON, for example {"window": 8}.')
@click.option("--until", type=int, help="Last round (no value uses data after its own round).")
@common
@click.pass_context
def indicator_series(ctx, run_id, indicator_id, params_json, until):
    """One indicator instance of one run (GET /runs/{id}/indicators/{indicator_id})."""
    json_arg(params_json, "--params")
    body = api(ctx).get(f"runs/{seg(run_id)}/indicators/{seg(indicator_id)}", params=params_json, until=until)

    def human(b):
        names = list(b["outputs"])
        rows = [{"t": t, **{n: b["outputs"][n][i] for n in names}} for i, t in enumerate(b["t"])]
        return table(rows, [col("t")] + [col(n) for n in names])

    emit(ctx, body, human)


COMMANDS = (runs, branches, indicators)
