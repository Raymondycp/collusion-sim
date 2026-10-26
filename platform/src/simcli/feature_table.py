"""Episode feature table of an experiment (contract 0.11.0, DECISIONS-10 §5): one row per cell, raw values, written to a file unchanged."""

from __future__ import annotations

import json

import click

from simcli.client import seg
from simcli.options import api, common
from simcli.output import emit, fields
from simcli.runs import save, writable


@click.group()
def features():
    """The episode feature table of an experiment (read only)."""


@features.command()
@click.argument("experiment_id")
@click.option("--layer", type=click.Choice(["A", "B"]), default="B", show_default=True, help="A: log-only features. B: every feature.")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json", show_default=True)
@click.option(
    "--evaluation",
    "evaluation_ids",
    multiple=True,
    help="Join the LLM judge scores of this evaluation (llm_* columns, meta.judge). Repeat it to merge evaluations that judge "
    "disjoint parents with the same item set, view and model, for example main and support_one_wording.",
)
@click.option("-o", "--output", required=True, type=click.Path(dir_okay=False), help="File to write.")
@click.option("--overwrite", is_flag=True, help="Replace an existing file (and its .meta.json for csv).")
@common
@click.pass_context
def export(ctx, experiment_id, layer, fmt, evaluation_ids, output, overwrite):
    """Write the feature table to a file (GET /experiments/{id}/features, one request). A null stays null; csv adds PATH.meta.json."""
    target = writable(output, overwrite)
    sidecar = writable(output + ".meta.json", overwrite) if fmt == "csv" else None
    table = api(ctx).raw(
        f"experiments/{seg(experiment_id)}/features", layer=layer, format="json", evaluation_id=list(evaluation_ids) or None
    )
    body = json.loads(table)
    meta = body["meta"]
    summary = {"experiment_id": experiment_id, "layer": layer, "format": fmt, "rows": len(body["rows"])}
    if evaluation_ids:
        summary["evaluation_ids"] = list(evaluation_ids)
    if sidecar is None:
        written = save(target, table)
    else:
        from features.table import to_csv  # the server's own csv writer, so the file equals ?format=csv of this response

        written = save(target, to_csv(body["columns"], body["rows"]).encode("utf-8"))
        summary["meta_path"] = save(sidecar, (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode("utf-8"))["path"]
    emit(ctx, {**written, **summary, "features_code_hash": meta["features_code_hash"], "features": len(meta["features"])}, fields)


COMMANDS = (features,)
