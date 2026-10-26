"""Judge jobs (contract 0.15.0, DECISIONS-9; resume from 0.15.0 CR-33): estimate, start, resume and show."""

from __future__ import annotations

import click

from simcli.client import CliError, seg
from simcli.options import api, common, json_file, paid, require_paid, writes
from simcli.output import col, emit, fields, table
from simcli.runs import Waiter, max_wait_option

PAID_BACKENDS = ("llm", "jev")  # the scripted judge makes no request
LIVE = ("queued", "running")
BACKEND_COLUMNS = [
    col("backend"),
    col("calls_planned"),
    col("calls_done"),
    col("charged"),
    col("tokens"),
    col("wall_ms"),
    ("counts", lambda r: " ".join(f"{k}={v}" for k, v in sorted(r["counts"].items()))),
]
ESTIMATE_COLUMNS = [
    col("backend"),
    col("calls"),
    col("tokens_in_est"),
    col("within_cap"),
    ("ledger used/held/cap", lambda r: "" if r["ledger"] is None else f"{r['ledger']['used']}/{r['ledger']['held']}/{r['ledger']['cap']}"),
]
EXCLUDED_COLUMNS = [col("index"), col("run_id"), col("reason")]


def backend_cell(e: dict, backend: str) -> str:
    j = e.get(backend)
    if j is None:
        return ""
    return f"{j['status']} agreement={j['agreement']} talk={j['talk_score']}"


def paid_guard(ctx, backends, allow_paid: bool) -> None:
    chosen = [b for b in PAID_BACKENDS if isinstance(backends, dict) and b in backends]
    if chosen:
        require_paid(ctx, allow_paid, f"Backends {', '.join(chosen)} call a model.")


def follow(ctx, job: dict, wait: bool, max_wait: float | None):
    """Print the job, or with --wait poll it until it is not queued or running. The id goes to stderr first, and a
    wait_timeout names it, so a job that is still running can be found again (CR-39 m1). A job that ends failed is
    exit 1 (evaluation_failed, with the job's error), like a run or experiment that cannot finish."""
    if not wait:
        return emit(ctx, job, evaluation_text)
    click.echo(f"evaluation {job['id']} {job['status']}; waiting", err=True)
    waiter = Waiter(ctx, max_wait)
    while True:
        body = api(ctx).get(f"evaluations/{seg(job['id'])}")
        if body["status"] == "failed":
            err = body.get("error") or {}
            msg = f"Evaluation {job['id']} failed: {err.get('code')}: {err.get('message', '')}"
            raise CliError(
                "evaluation_failed",
                msg,
                body={"code": "evaluation_failed", "status": None, "message": msg, "evaluation_id": job["id"], "evaluation_error": err},
            )
        if body["status"] not in LIVE:
            return emit(ctx, body, evaluation_text)
        try:
            waiter.pause()
        except CliError as e:
            msg = f"Stopped after --max-wait {max_wait:g} s; evaluation {job['id']} is still {body['status']}."
            raise CliError("wait_timeout", msg, body=e.body | {"message": msg, "evaluation_id": job["id"]}) from None


def evaluation_text(ev: dict) -> str:
    head = {k: ev[k] for k in ("id", "status", "block", "view", "order_seed", "created_at", "started_at", "finished_at") if k in ev}
    head["item_set"] = f"{ev['item_set']['id']} {ev['item_set']['hash']}"
    if ev.get("error"):
        head["error"] = ev["error"].get("code")
    rows = [{"backend": b, **r} for b, r in sorted(ev["per_backend"].items())]
    parts = [fields(head), table(rows, BACKEND_COLUMNS)]
    backends = sorted(ev["backends"])
    columns = [col("run", "run_id"), col("condition", "condition_id"), col("split"), col("seed")]
    columns += [(b, lambda e, b=b: backend_cell(e, b)) for b in backends]
    if ev.get("episodes"):
        parts.append(table(ev["episodes"], columns))
    return "\n\n".join(parts)


def estimate_text(e: dict) -> str:
    rows = [{"backend": b, **r} for b, r in sorted(e["per_backend"].items())]
    parts = [fields({"episodes": e["episodes"], "order_seed": e["order_seed"]}), table(rows, ESTIMATE_COLUMNS)]
    if e["excluded"]:
        parts.append("excluded\n" + table(e["excluded"], EXCLUDED_COLUMNS))
    parts.append("\n".join(f"warning: {w}" for w in e["warnings"]))
    parts.append("\n".join(f"- {a}" for a in e["assumptions"]))
    return "\n\n".join(p for p in parts if p)


@click.group()
def evaluations():
    """Judge jobs on completed parent runs: estimate, start, show."""


@evaluations.command()
@click.option("--file", "request_file", required=True, help="JSON file with an EvaluationCreate request.")
@common
@click.pass_context
def estimate(ctx, request_file):
    """Calls, tokens and ledger room of a judge job (POST /evaluations/estimate). Every start check; no call, stores nothing."""
    emit(ctx, api(ctx).post("evaluations/estimate", json_file(request_file, "evaluation file")), estimate_text)


@evaluations.command()
@click.option("--file", "request_file", required=True, help="JSON file with an EvaluationCreate request.")
@click.option("--wait", is_flag=True, help="Poll until the job is completed, stopped or failed; print it.")
@max_wait_option
@paid
@writes
@common
@click.pass_context
def start(ctx, request_file, wait, max_wait, allow_paid):
    """Start a judge job (POST /evaluations). An llm or jev backend needs --allow-paid; scripted is free."""
    request = json_file(request_file, "evaluation file")
    paid_guard(ctx, request.get("backends") if isinstance(request, dict) else None, allow_paid)
    follow(ctx, api(ctx).post("evaluations", request), wait, max_wait)


@evaluations.command()
@click.argument("evaluation_id")
@click.option("--wait", is_flag=True, help="Poll until the job is completed, stopped or failed; print it.")
@max_wait_option
@paid
@writes
@common
@click.pass_context
def resume(ctx, evaluation_id, wait, max_wait, allow_paid):
    """Run a failed job again under its id (POST /evaluations/{id}/resume). A job with an llm or jev backend needs --allow-paid."""
    paid_guard(ctx, api(ctx).get(f"evaluations/{seg(evaluation_id)}")["backends"], allow_paid)
    follow(ctx, api(ctx).post(f"evaluations/{seg(evaluation_id)}/resume"), wait, max_wait)


@evaluations.command()
@click.argument("evaluation_id")
@common
@click.pass_context
def show(ctx, evaluation_id):
    """One judge job: progress per backend and each episode's judgement (GET /evaluations/{id})."""
    emit(ctx, api(ctx).get(f"evaluations/{seg(evaluation_id)}"), evaluation_text)


COMMANDS = (evaluations,)
