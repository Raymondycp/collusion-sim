"""`serve`: run the backend in this process, the same as `uv run python -m api`."""

from __future__ import annotations

import os
import runpy
from pathlib import Path

import click

from adapters.server_lock import DataFolderInUse, ServerLock
from services.profiles import paid_calls_allowed
from simcli.client import CliError


@click.command()
@click.option("--port", type=int, help="Port on 127.0.0.1 (env SIM_PORT, default 8000).")
@click.option("--root", type=click.Path(file_okay=False), help="SIM_ROOT: the folder with scenarios/, data/, runs/ (default: this folder).")
@click.option("--durable", is_flag=True, help="SIM_DURABLE=1: restart unfinished runs with DBOS. Off by default (the local path).")
@click.option("--no-durable", is_flag=True, help="Kept for scripts: no DBOS is already the default.")
def serve(port, root, durable, no_durable):
    """Start the API server in the foreground (Ctrl-C stops it). It never turns paid calls on."""
    if port:
        os.environ["SIM_PORT"] = str(port)
    if root:
        os.environ["SIM_ROOT"] = os.path.abspath(root)
    if durable:
        os.environ["SIM_DURABLE"] = "1"
    elif no_durable or "SIM_DURABLE" not in os.environ:
        os.environ["SIM_DURABLE"] = "0"
    base = Path(os.environ.get("SIM_ROOT") or Path.cwd())
    try:
        ServerLock(base / "data").check()
    except DataFolderInUse as e:
        raise CliError("data_folder_in_use", str(e)) from None
    paid = "on (SIM_ALLOW_PAID=1)" if paid_calls_allowed(base / ".env") else "off"
    click.echo(f"collusion-sim serve: 127.0.0.1:{os.environ.get('SIM_PORT', '8000')}, paid calls {paid}", err=True)
    runpy.run_module("api", run_name="__main__", alter_sys=True)


COMMANDS = (serve,)
