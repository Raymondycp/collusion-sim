"""Provider profiles and keys (CLI-DESIGN §3.4, §5). A key comes only from stdin, goes in one request body, and is never printed."""

from __future__ import annotations

import getpass
import sys

import click

from simcli.client import CliError, seg
from simcli.options import api, common, paid, require_paid, writes
from simcli.output import col, emit, fields, table

PROFILE_COLUMNS = [col("id"), col("name"), col("base_url"), col("key_set"), col("key_hint"), col("default_model"), col("max_concurrency")]


NOT_A_TTY = "Reading the API key from stdin. Typed input is visible here; pipe it from a file or a secret store."


def read_key() -> str:
    """On a TTY: a hidden prompt. Otherwise: one stdin line, after a warning on stderr (CR-7 m1). Never an option, argument or env var."""
    if sys.stdin.isatty():
        key = getpass.getpass("API key (hidden): ")
    else:
        click.echo(NOT_A_TTY, err=True)
        key = sys.stdin.readline()
    key = key.strip()
    if not key:
        raise CliError("cli_usage", "No API key on stdin. Pipe it in, or type it at the hidden prompt.", exit_code=2)
    return key


def profile_fields(name, base_url, default_model, max_concurrency, input_price, output_price) -> dict:
    if (input_price is None) != (output_price is None):
        raise click.UsageError("Give --input-price and --output-price together.")
    body = {"name": name, "base_url": base_url, "default_model": default_model, "max_concurrency": max_concurrency}
    if input_price is not None:
        body["pricing"] = {"input_usd_per_mtok": input_price, "output_usd_per_mtok": output_price}
    return {k: v for k, v in body.items() if v is not None}


def profile_options(required: bool):
    def decorate(f):
        for option in reversed(
            [
                click.option("--name", required=required),
                click.option("--base-url", required=required, help="OpenAI-compatible base URL, for example https://api.example.com/v1."),
                click.option("--default-model"),
                click.option("--max-concurrency", type=int, help="Model calls in flight for this profile, across all runs."),
                click.option("--input-price", type=float, help="USD per million input tokens (optional; with --output-price)."),
                click.option("--output-price", type=float, help="USD per million output tokens (optional; with --input-price)."),
            ]
        ):
            f = option(f)
        return f

    return decorate


@click.group()
def profiles():
    """Provider profiles. Keys stay on the server; responses show key_set and key_hint only."""


@profiles.command("list")
@common
@click.pass_context
def list_profiles(ctx):
    """Profiles (GET /provider-profiles)."""
    emit(ctx, api(ctx).get("provider-profiles"), lambda rows: table(rows, PROFILE_COLUMNS))


@profiles.command()
@click.argument("profile_id")
@common
@click.pass_context
def show(ctx, profile_id):
    """One profile (GET /provider-profiles/{id})."""
    emit(ctx, api(ctx).get(f"provider-profiles/{seg(profile_id)}"))


@profiles.command()
@profile_options(required=True)
@common
@click.pass_context
def add(ctx, **options):
    """Create a profile (POST /provider-profiles). The key is read from stdin, never from an argument."""
    body = profile_fields(**options)
    emit(ctx, api(ctx).post("provider-profiles", {**body, "api_key": read_key()}))


@profiles.command("set-key")
@click.argument("profile_id")
@common
@click.pass_context
def set_key(ctx, profile_id):
    """Replace the key (PATCH /provider-profiles/{id} with api_key only). The key is read from stdin."""
    emit(ctx, api(ctx).patch(f"provider-profiles/{seg(profile_id)}", {"api_key": read_key()}))


@profiles.command()
@click.argument("profile_id")
@profile_options(required=False)
@writes
@common
@click.pass_context
def update(ctx, profile_id, **options):
    """Change fields other than the key (PATCH /provider-profiles/{id})."""
    body = profile_fields(**options)
    if not body:
        raise click.UsageError("Give at least one field to change.")
    emit(ctx, api(ctx).patch(f"provider-profiles/{seg(profile_id)}", body))


@profiles.command()
@click.argument("profile_id")
@writes
@common
@click.pass_context
def delete(ctx, profile_id):
    """Delete the profile and its stored key (DELETE /provider-profiles/{id}). A profile in use gives profile_in_use."""
    api(ctx).delete(f"provider-profiles/{seg(profile_id)}")
    emit(ctx, {"deleted": profile_id})


@profiles.command()
@click.argument("profile_id")
@common
@click.pass_context
def models(ctx, profile_id):
    """Model ids from the endpoint's GET /models (the server calls the provider with the stored key; not billed)."""
    emit(ctx, api(ctx).get(f"provider-profiles/{seg(profile_id)}/models"), fields)


@profiles.command("test")
@click.argument("profile_id")
@click.option("--model", help="Default: the profile's default_model.")
@click.option(
    "--thinking",
    type=click.Choice(["true", "false", "minimal", "low", "medium", "high", "xhigh"]),
    help="The unified thinking setting for this call. Default: unset (the model's own default).",
)
@paid
@common
@click.pass_context
def test_profile(ctx, profile_id, model, thinking, allow_paid):
    """One small paid call to the endpoint (POST /provider-profiles/{id}/test). Needs --allow-paid and the server switch."""
    require_paid(ctx, allow_paid, "A profile test is one model call.")
    body: dict = {"model": model} if model else {}
    if thinking is not None:
        body["thinking"] = {"true": True, "false": False}.get(thinking, thinking)
    emit(ctx, api(ctx).post(f"provider-profiles/{seg(profile_id)}/test", body), fields)


COMMANDS = (profiles,)
