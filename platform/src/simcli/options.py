"""Options every command shares, and the per-invocation API client (kept on the root context object)."""

from __future__ import annotations

import json
from pathlib import Path

import click

from simcli.client import DEFAULT_SERVER, Api, CliError, connect


def store(name: str):
    def callback(ctx: click.Context, param: click.Parameter, value):
        if value is not None and value is not False:
            ctx.find_root().obj[name] = value

    return callback


def common(f):
    """--json, --server and --timeout, accepted before or after the subcommand."""
    f = click.option("--timeout", type=float, expose_value=False, callback=store("timeout"), help="Seconds per request (default 30).")(f)
    f = click.option(
        "--server", expose_value=False, callback=store("server"), help=f"API base URL (env SIM_API_URL, default {DEFAULT_SERVER})."
    )(f)
    return click.option("--json", is_flag=True, expose_value=False, callback=store("json"), help="Print the API body as JSON.")(f)


def writes(f):
    """--dry-run on a write command: print the write request, send no write (reads still run)."""
    return click.option(
        "--dry-run", is_flag=True, expose_value=False, callback=store("dry_run"), help="Print the request; send no write request."
    )(f)


def paid(f):
    return click.option("--allow-paid", is_flag=True, help="Allow a command that can make paid model calls (needs the user's approval).")(f)


def require_paid(ctx: click.Context, allowed: bool, why: str) -> None:
    """The CLI's own lock on paid calls (CLI-DESIGN §6). The server's SIM_ALLOW_PAID switch still decides. A dry run sends no write."""
    if not allowed and not ctx.find_root().obj.get("dry_run"):
        raise CliError("paid_flag_required", f"{why} This can make paid model calls: pass --allow-paid, only with the user's approval.")


def api(ctx: click.Context) -> Api:
    obj = ctx.find_root().obj
    if "api" not in obj:
        http = obj["http"] if "http" in obj else connect(obj["server"], obj.get("timeout", 30.0))
        obj["api"] = Api(http, dry_run=obj.get("dry_run", False))
    return obj["api"]


def json_arg(text: str | None, what: str):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as e:
        raise CliError("file_invalid", f"{what} is not JSON: {e}.") from None


def json_file(path: str, what: str):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")  # PowerShell 5.1 writes a BOM (CR-44 4)
    except OSError as e:
        raise CliError("file_invalid", f"Cannot read {what} {path}: {e.strerror}.") from None
    return json_arg(text, f"{what} {path}")
