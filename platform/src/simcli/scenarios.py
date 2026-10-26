"""Scenarios and the market preview (CLI-DESIGN §3.3)."""

from __future__ import annotations

import click

from simcli.client import seg
from simcli.options import api, common, json_file, paid, require_paid, writes
from simcli.output import col, emit, table

SCENARIO_COLUMNS = [col("id"), col("name"), col("status"), col("n_sellers"), col("n_events"), col("hash"), col("updated_at")]


@click.group()
def scenarios():
    """Scenarios: drafts, generators, checks, freeze, reference values."""


@scenarios.command("list")
@click.option("--status", type=click.Choice(["draft", "frozen"]))
@common
@click.pass_context
def list_scenarios(ctx, status):
    """Scenarios (GET /scenarios)."""
    emit(ctx, api(ctx).get("scenarios", status=status), lambda rows: table(rows, SCENARIO_COLUMNS))


@scenarios.command()
@click.argument("scenario_id")
@common
@click.pass_context
def show(ctx, scenario_id):
    """One scenario (GET /scenarios/{id})."""
    emit(ctx, api(ctx).get(f"scenarios/{seg(scenario_id)}"))


@scenarios.command()
@click.option("--file", "draft_file", required=True, help="JSON file with a ScenarioDraft.")
@writes
@common
@click.pass_context
def create(ctx, draft_file):
    """Save a new draft (POST /scenarios)."""
    emit(ctx, api(ctx).post("scenarios", json_file(draft_file, "draft file")))


@scenarios.command()
@click.argument("scenario_id")
@click.option("--file", "draft_file", required=True, help="JSON file with a ScenarioDraft.")
@writes
@common
@click.pass_context
def replace(ctx, scenario_id, draft_file):
    """Replace a draft (PUT /scenarios/{id}). A frozen scenario gives scenario_frozen."""
    emit(ctx, api(ctx).put(f"scenarios/{seg(scenario_id)}", json_file(draft_file, "draft file")))


@scenarios.command()
@click.argument("scenario_id")
@writes
@common
@click.pass_context
def delete(ctx, scenario_id):
    """Delete a draft (DELETE /scenarios/{id})."""
    api(ctx).delete(f"scenarios/{seg(scenario_id)}")
    emit(ctx, {"deleted": scenario_id})


@scenarios.command()
@click.argument("generator_id")
@click.option("--inputs", "inputs_file", required=True, help="JSON file with the generator inputs (see generators show).")
@click.option("--seed", type=int)
@click.option("--profile", "provider_profile_id", help="Provider profile (llm generator only).")
@click.option("--save", is_flag=True, help="Also save the draft (POST /scenarios).")
@paid
@writes
@common
@click.pass_context
def generate(ctx, generator_id, inputs_file, seed, provider_profile_id, save, allow_paid):
    """Run a generator (POST /scenarios/generate). It returns a draft and its check report and saves nothing."""
    body = {"generator_id": generator_id, "inputs": json_file(inputs_file, "inputs file"), "seed": seed}
    if provider_profile_id:
        body["provider_profile_id"] = provider_profile_id
    known = {g["id"]: g for g in api(ctx).get("scenario-generators")}
    if known.get(generator_id, {}).get("paid"):
        require_paid(ctx, allow_paid, f"Generator {generator_id} is paid (GET /scenario-generators).")
    out = api(ctx).post("scenarios/generate", {k: v for k, v in body.items() if v is not None})
    if save:
        out = {"draft": out, "saved": api(ctx).post("scenarios", out["draft"])}
    emit(ctx, out)


@scenarios.command()
@click.argument("scenario_id")
@common
@click.pass_context
def check(ctx, scenario_id):
    """Run the automatic checks on a draft (POST /scenarios/{id}/check). No LLM call."""
    report = api(ctx).post(f"scenarios/{seg(scenario_id)}/check")
    emit(ctx, report, lambda r: f"ok: {r['ok']}\n" + table(r["checks"], [col("id"), col("ok"), col("message")]))


@scenarios.command()
@click.argument("scenario_id")
@click.option("--approved-by", required=True, help="The person who approved the draft. Only a name the user gave; never invent one.")
@writes
@common
@click.pass_context
def freeze(ctx, scenario_id, approved_by):
    """Check, hash and make the draft read-only (POST /scenarios/{id}/freeze)."""
    emit(ctx, api(ctx).post(f"scenarios/{seg(scenario_id)}/freeze", {"approved_by": approved_by}))


@scenarios.command()
@click.argument("scenario_id")
@click.option("--until", type=int, help="Last round.")
@common
@click.pass_context
def reference(ctx, scenario_id, until):
    """params(t), costs, Nash(t), joint optimum(t) and event markers (GET /scenarios/{id}/reference)."""
    body = api(ctx).get(f"scenarios/{seg(scenario_id)}/reference", until=until)
    emit(ctx, body, lambda b: table(b["rounds"], [col("t"), col("nash"), col("joint_optimum"), col("params")]))


@click.group()
def market():
    """Market calculations with no LLM."""


@market.command()
@click.option("--file", "market_file", required=True, help="JSON file: market, n_sellers, rounds, events.")
@common
@click.pass_context
def preview(ctx, market_file):
    """Reference prices per round for a draft market (POST /market/preview). Pure calculation."""
    out = api(ctx).post("market/preview", json_file(market_file, "market file"))
    emit(ctx, out, lambda b: table(b["rounds"], [col("t"), col("nash"), col("joint_optimum")]))


COMMANDS = (scenarios, market)
