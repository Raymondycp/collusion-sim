"""Offline commands: read an episode.jsonl file with simschema only. No server, no write."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import click
from pydantic import ValidationError

from simcli.client import CliError
from simcli.options import common
from simcli.output import emit, fields
from simschema.episode import EpisodeFormatError, read_episode
from simschema.events import SIM_EVENT, EpisodeHeader


def _first_bad_line(lines: list[str]) -> dict:
    for number, line in enumerate(lines, start=1):
        if number > 1 and not line.strip():
            continue
        try:
            (EpisodeHeader.model_validate_json if number == 1 else SIM_EVENT.validate_json)(line)
        except ValidationError as e:
            first = e.errors()[0]
            where = "/".join(str(p) for p in first["loc"])
            return {"path": f"line {number}", "message": f"{where}: {first['msg']}" if where else first["msg"]}
    return {"path": "", "message": "no single bad line"}


def load(path: str):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise CliError("file_invalid", f"Cannot read {path}: {e}.") from None
    if not lines:
        raise CliError("episode_invalid", f"{path} is empty.", errors=[{"path": "line 1", "message": "no header line"}])
    try:
        return read_episode(lines)
    except ValidationError:
        raise CliError("episode_invalid", f"{path} has a line that is not schema-valid.", errors=[_first_bad_line(lines)]) from None
    except EpisodeFormatError as e:
        raise CliError("episode_invalid", f"{path}: {e}.", errors=[{"path": "", "message": str(e)}]) from None


@click.group()
def episode():
    """Offline checks of an episode.jsonl file (no server needed)."""


@episode.command()
@click.argument("path", type=click.Path(dir_okay=False))
@common
@click.pass_context
def validate(ctx, path):
    """Check the header, the gap-free seq, run_started first, run_ended last, and every event's schema."""
    header, events = load(path)
    emit(
        ctx,
        {
            "ok": True,
            "path": path,
            "run_id": header.run_id,
            "schema_version": header.schema_version,
            "events": len(events),
            "rounds": sum(e.type == "round_started" for e in events),
            "last_seq": events[-1].seq,
        },
        fields,
    )


@episode.command()
@click.argument("path", type=click.Path(dir_okay=False))
@common
@click.pass_context
def summary(ctx, path):
    """Counts by event type and tier, rounds, agents and the end reason."""
    header, events = load(path)
    emit(
        ctx,
        {
            "path": path,
            "run_id": header.run_id,
            "kind": header.kind,
            "condition_id": header.condition_id,
            "rounds": sum(e.type == "round_started" for e in events),
            "agents": [a.id for a in header.agents],
            "by_type": dict(sorted(Counter(e.type for e in events).items())),
            "by_tier": dict(sorted(Counter(e.tier for e in events).items())),
            "end_reason": events[-1].end_reason,
        },
        fields,
    )


COMMANDS = (episode,)
