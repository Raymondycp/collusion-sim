"""Server status, registries and the ledger: read-only commands."""

from __future__ import annotations

import click

from simcli.client import CliError, seg
from simcli.options import api, common, json_file
from simcli.output import col, emit, fields, table


@click.command()
@common
@click.pass_context
def health(ctx):
    """Backend status and versions (GET /health)."""
    emit(ctx, api(ctx).get("health"))


def registry(name: str, path: str, columns, key: str, help_text: str) -> click.Group:
    """A read-only registry: `list` prints the API list, `show ID` one item of it."""

    @click.group(name=name, help=help_text)
    def group():
        pass

    @group.command("list")
    @common
    @click.pass_context
    def list_(ctx):
        emit(ctx, api(ctx).get(path), lambda rows: table(rows, columns))

    list_.help = f"List the registry (GET /{path})."

    @group.command("show")
    @click.argument("item_id")
    @common
    @click.pass_context
    def show(ctx, item_id):
        for item in api(ctx).get(path):
            if item.get(key) == item_id:
                return emit(ctx, item)
        raise CliError("not_found", f"No {name} entry with {key} {item_id}.")

    show.help = f"One entry of GET /{path}, by {key}."
    return group


demand_models = registry(
    "demand-models",
    "demand-models",
    [col("id"), col("version"), col("name"), col("research_allowed"), col("references")],
    "id",
    "Demand models (GET /demand-models) and the demand helper.",
)


@demand_models.command()
@click.argument("model_id")
@click.option("--targets", "targets_file", required=True, help="JSON file with the solver input (see solver_schema).")
@common
@click.pass_context
def solve(ctx, model_id, targets_file):
    """Target prices in, params out (POST /demand-models/{id}/solve). No LLM."""
    emit(ctx, api(ctx).post(f"demand-models/{seg(model_id)}/solve", json_file(targets_file, "targets file")))


roles = registry(
    "roles",
    "roles",
    [col("name"), col("stage"), col("built"), col("research_allowed"), col("phases")],
    "name",
    "Agent roles (GET /roles).",
)
tools = registry(
    "tools",
    "tools",
    [col("id"), col("version"), col("kind"), col("data_layer"), col("roles")],
    "id",
    "Tools an agent can be granted (GET /tools).",
)
policies = registry(
    "policies", "policies", [col("kind"), col("name"), col("roles")], "kind", "Policy kinds, scripted and LLM (GET /policies)."
)
generators = registry(
    "generators",
    "scenario-generators",
    [col("id"), col("version"), col("name"), col("paid"), col("deterministic")],
    "id",
    "Scenario generators (GET /scenario-generators).",
)


@click.group()
def ledger():
    """The append-only, hash-chained run ledger."""


@ledger.command("list")
@click.option("--limit", type=int)
@click.option("--cursor")
@common
@click.pass_context
def ledger_list(ctx, limit, cursor):
    """Ledger lines (GET /ledger)."""
    columns = [col("line"), col("run_id"), col("kind"), col("status"), col("condition_id"), col("split"), col("seed"), col("hash")]
    emit(ctx, api(ctx).get("ledger", limit=limit, cursor=cursor), lambda b: table(b["items"], columns) + _more(b))


@ledger.command()
@common
@click.pass_context
def verify(ctx):
    """Check the hash chain (GET /ledger/verify)."""
    emit(ctx, api(ctx).get("ledger/verify"), fields)


def _more(page: dict) -> str:
    return f"\nnext cursor: {page['next_cursor']}" if page.get("next_cursor") else ""


COMMANDS = (health, demand_models, roles, tools, policies, generators, ledger)
