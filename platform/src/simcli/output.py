"""Rendering. --json prints the body unchanged; the default is plain text with no colour (CLI-DESIGN §4)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import click

Column = tuple[str, Callable[[dict], Any]]


def col(header: str, key: str | None = None) -> Column:
    return header, lambda row: row.get(key or header)


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def table(rows: Sequence[dict], columns: Sequence[Column]) -> str:
    grid = [[h for h, _ in columns]] + [[cell(f(r)) for _, f in columns] for r in rows]
    widths = [max(len(line[i]) for line in grid) for i in range(len(columns))]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip() for line in grid)


def fields(obj: dict) -> str:
    width = max((len(k) for k in obj), default=0)
    return "\n".join(f"{k.ljust(width)}  {cell(v)}" for k, v in obj.items())


def dumps(data: Any, *, indent: int | None = 2) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False)


def emit(ctx: click.Context, data: Any, human: Callable[[Any], str] | None = None) -> None:
    if ctx.find_root().obj["json"]:
        click.echo(dumps(data))
    else:
        click.echo(human(data) if human else fields(data) if isinstance(data, dict) else cell(data))


def problem_lines(body: dict) -> str:
    status = body.get("status")
    head = f"error {body.get('code')}" + (f" (HTTP {status})" if status else "") + f": {body.get('message', '')}"
    return "\n".join([head] + [f"  {e.get('path', '')}: {e.get('message', '')}" for e in body.get("errors") or []])
