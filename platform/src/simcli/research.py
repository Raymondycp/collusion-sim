"""Research setup: conditions, the seed map and the rule freeze. Reads, plus `freeze run`; files are committed in git."""

from __future__ import annotations

import click

from simcli.client import seg
from simcli.options import api, common, writes
from simcli.output import col, emit, fields, table

CONDITION_COLUMNS = [
    col("id"),
    col("version"),
    col("status"),
    col("group"),
    col("sub_condition"),
    col("tag"),
    ("legal", lambda c: (c.get("labels") or {}).get("legal")),
    ("harm", lambda c: (c.get("labels") or {}).get("harm")),
    col("name"),
    col("hash"),
]
SPLITS = ("pilot", "dev", "test")


SEED_COLUMNS = [col("seed"), col("split"), col("shock_news"), col("informed_seller"), col("scenario_id"), col("scenario_hash")]


def balance(entries: list[dict], split: str) -> str:
    """Design balance of one split: public and private news, and the informed seller of each private seed."""
    mine = [e for e in entries if e.get("split") == split]
    private = [e for e in mine if e.get("shock_news") == "private"]
    informed = {
        s: sum(e.get("informed_seller") == s for e in private) for s in sorted({e.get("informed_seller") for e in private} - {None})
    }
    return f"public {len(mine) - len(private)}, private {len(private)}" + "".join(f", {s} {n}" for s, n in informed.items())


def seed_lines(body: dict) -> str:
    entries = body.get("seeds")
    rows = [{"split": s, "n": len(body[s]), "seeds": ", ".join(map(str, body[s]))} for s in SPLITS]
    columns = [col("split"), col("n"), col("seeds")]
    if entries is not None:
        rows = [{**r, "balance": balance(entries, r["split"])} for r in rows]
        columns.insert(2, col("balance"))
    text = f"{table(rows, columns)}\n\nhash         {body['hash']}\ntest_locked  {body['test_locked']}"
    return text if entries is None else f"{text}\n\n{table(entries, SEED_COLUMNS)}"


@click.group()
def conditions():
    """Condition files (conditions/*.yaml), resolved. Read only: they are edited in git."""


@conditions.command("list")
@common
@click.pass_context
def list_conditions(ctx):
    """Every condition (GET /conditions)."""
    emit(ctx, api(ctx).get("conditions"), lambda rows: table(rows, CONDITION_COLUMNS))


@conditions.command()
@click.argument("condition_id")
@common
@click.pass_context
def show(ctx, condition_id):
    """One condition with its labels, capabilities and prompt variants (GET /conditions/{id})."""
    emit(ctx, api(ctx).get(f"conditions/{seg(condition_id)}"), fields)


@click.group()
def seeds():
    """The seed map (conditions/seeds.yaml). Read only: it is edited in git."""


@seeds.command("list")
@common
@click.pass_context
def list_seeds(ctx):
    """Pilot, dev and test seeds and the map's hash (GET /seeds). seeds_missing if the file does not exist."""
    emit(ctx, api(ctx).get("seeds"), seed_lines)


@click.group()
def freeze():
    """The rule freeze (frozen.json, owned by P2): status, and the one-step freeze."""


@freeze.command()
@common
@click.pass_context
def status(ctx):
    """Whether the rule is frozen and every stored hash still matches the files (GET /freeze)."""
    emit(ctx, api(ctx).get("freeze"), fields)


@freeze.command("run")
@click.option("--stage", type=click.Choice(["conditions", "rule"]), required=True, help="conditions opens dev; rule (after it) opens test.")
@click.option("--approved-by", required=True, help="The person who approved the freeze. Only a name the user gave; never invent one.")
@writes
@common
@click.pass_context
def run_freeze(ctx, stage, approved_by):
    """Freeze one stage of the research inputs (POST /freeze). It writes the condition files and frozen.json; it never commits."""
    emit(ctx, api(ctx).post("freeze", {"stage": stage, "approved_by": approved_by}), fields)


COMMANDS = (conditions, seeds, freeze)
