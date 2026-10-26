"""collusion-sim: agent-native CLI client of the simulator API (docs/sim/CLI-DESIGN.md)."""

from __future__ import annotations

import re
import sys

import click

from simcli import catalog, estimates, evaluations, experiments, feature_table, offline, profiles, research, runs, scenarios, schema, server
from simcli.client import DEFAULT_SERVER, CliError, DryRun, hub_keys
from simcli.options import store
from simcli.output import dumps, fields, problem_lines


def secret_like(token: str) -> bool:
    return len(token) >= 16 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


KEY_COMMANDS = {"add", "set-key"}


def key_command(args: list[str]) -> bool:
    return any(a == "profiles" and b in KEY_COMMANDS for a, b in zip(args, args[1:]))


def redact_values(message: str, args: list[str]) -> str:
    """A usage error must not echo a key that was typed as an argument by mistake (CLI-DESIGN §5, CR-7 m2).

    `profiles add` and `profiles set-key`: every argument value is hidden, whatever its shape. Other commands: values that look
    like a secret (16 or more characters with letters and digits)."""
    hide_all = key_command(args)
    for token in args:
        value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        if not value or value.startswith("-") or (hide_all and value in {"profiles", *KEY_COMMANDS}):
            continue
        if hide_all or secret_like(value):
            message = re.sub(rf"(?<![\w-]){re.escape(value)}(?![\w-])", "(value not shown)", message)
    return message


class SimGroup(click.Group):
    """Errors follow CLI-DESIGN §4.3: the Problem goes to stdout with --json, a short line to stderr, and the exit code is 1, 2 or 3."""

    def main(self, args=None, prog_name=None, complete_var=None, standalone_mode=True, **extra):
        for stream in (sys.stdout, sys.stderr):  # UTF-8 on every console code page (SE-integration B1)
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        args = list(sys.argv[1:] if args is None else args)
        obj = {"json": False, **(extra.pop("obj", None) or {})}

        def as_json() -> bool:
            return obj["json"] or "--json" in args

        try:
            rv = super().main(args, prog_name or "collusion-sim", complete_var, standalone_mode=False, obj=obj, **extra)
        except DryRun as d:
            click.echo(dumps(d.payload) if as_json() else fields(d.payload))
            sys.exit(0)
        except CliError as e:
            self._fail(e.body, e.exit_code, as_json())
        except click.ClickException as e:
            e.message = redact_values(e.message, args)
            if as_json():
                click.echo(dumps(hub_keys({"code": "cli_usage", "status": None, "message": e.format_message()}), indent=None))
            e.show()
            sys.exit(e.exit_code)
        except click.Abort:
            self._fail({"code": "cli_aborted", "status": None, "message": "Aborted (Ctrl-C)."}, 1, as_json())
        sys.exit(rv if isinstance(rv, int) else 0)

    @staticmethod
    def _fail(body: dict, exit_code: int, as_json: bool):
        if as_json:
            click.echo(dumps(hub_keys(body), indent=None))
        click.echo(problem_lines(body), err=True)
        sys.exit(exit_code)


@click.group(cls=SimGroup, invoke_without_command=True)
@click.option("--server", envvar="SIM_API_URL", default=DEFAULT_SERVER, expose_value=False, callback=store("server"), help="API base URL.")
@click.option("--timeout", type=float, default=30.0, expose_value=False, callback=store("timeout"), help="Seconds per request.")
@click.option("--json", is_flag=True, expose_value=False, callback=store("json"), help="Print JSON (the API body, unchanged).")
@click.pass_context
def cli(ctx: click.Context):
    """Client of the collusion simulator API. Same data as the web UI. --json on every command."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


MODULES = (catalog, estimates, schema, profiles, scenarios, research, experiments, evaluations, feature_table, runs, offline, server)
for command in (c for m in MODULES for c in m.COMMANDS):
    cli.add_command(command)
