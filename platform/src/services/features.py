"""The episode feature table of an experiment (DECISIONS-10 §5): one row per cell, built from the stored events.

Label and provenance columns come from the parent's header and are added after the features are computed; the
features never see them (CR-17 U4-1). Group comes from the condition file, which must still have the hash the run
recorded. Test rows need a valid freeze (CR-17 U4-6).

Judge columns (item 3 unit J4) come from one or more named evaluations: their LLM aggregation per parent episode, from `ok`
calls only. Several evaluations merge when they share the item set, view, transform and LLM model and judge disjoint parents
(DECISIONS-13 §7: `main` and `support_one_wording` together cover every group; CR-64 finding 2). Each row names the
evaluation its judge columns come from. The judge never saw the labels; the columns are joined by run id after the judging.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from adapters.store import RunsDB
from features import REGISTRY, ScenarioFacts
from features.base import JUDGE_CODE, DataLayer, files_hash
from features.table import Child, feature_columns, row_values, to_csv
from judge.blocks import BLOCKS
from judge.scoring import JUDGE_FEATURES, invalid_rate_by_group, judge_columns
from services.catalog import Catalog
from services.configs import ApiError
from services.research import Research
from simcore.branch import BranchRequest
from simcore.run import RunSpec
from simschema.events import EpisodeHeader

ID_COLUMNS = (
    "experiment_id",
    "condition_id",
    "group",
    "sub_condition",
    "label_legal",
    "label_harm",
    "split",
    "seed",
    "parent_run_id",
    "parent_status",
    "child_run_ids",
    "child_kinds",
    "config_hash",
    "condition_hash",
    "seeds_hash",
)


def _judge_specs(evaluations: list[dict]) -> list[dict]:
    first = evaluations[0]
    inputs = ["posted", "settled"] if first["view"] == "talk_prices" else ["posted"]
    code = files_hash(JUDGE_CODE)
    source = "; ".join(f"{e['id']} ({e['block']})" for e in evaluations)
    return [
        {
            "id": f,
            "version": "1",
            "name": name,
            "description": f"{text}. From evaluation {source}, view {first['view']}.",
            "evidence_type": "conversation",
            "data_layer": "A",
            "access": "log",
            "inputs": inputs,
            "scenario": False,
            "branches": [],
            "code_hash": code,
        }
        for f, (name, text) in JUDGE_FEATURES.items()
    ]


MERGE_KEYS = {
    "item set": lambda e: (e["item_set"]["id"], e["item_set"]["hash"]),
    "view": lambda e: (e["view"], BLOCKS[e["block"]].transform),
    "LLM model": lambda e: (e["backends"]["llm"]["model"], e["backends"]["llm"]["model_family"]),
}
"""What evaluations must share to fill one set of judge columns (CR-64 finding 2, option a)."""


def check_merge(evaluations: list[dict]) -> None:
    """422 path evaluation_id/<i> for an evaluation with no llm backend, one that differs from the first in a MERGE_KEYS value,
    or one that judges a parent another evaluation also judged."""

    def refuse(i: int, message: str) -> ApiError:
        path = [{"path": f"evaluation_id/{i}", "message": message}]
        return ApiError(422, "validation_error", f"Evaluation {evaluations[i]['id']}: {message}.", path)

    seen: dict[str, str] = {}
    for i, e in enumerate(evaluations):
        if "llm" not in e["backends"]:
            raise refuse(i, "no llm backend; judge columns come from the LLM judge only")
        for name, key in MERGE_KEYS.items():
            if key(e) != key(evaluations[0]):
                raise refuse(i, f"its {name} differs from evaluation {evaluations[0]['id']}")
        for rid in e["run_ids"]:
            if rid in seen:
                raise refuse(i, f"parent {rid} is also judged by evaluation {seen[rid]}")
            seen[rid] = e["id"]


def group_key(row: dict) -> str:
    """The group as the P2 report splits it: C3 by sub-condition (C3-accept, C3-silent), other groups by `group`."""
    sub = {"responder_accepts": "accept", "responder_silent": "silent"}.get(row["sub_condition"] or "", row["sub_condition"])
    return f"{row['group']}-{sub}" if sub else row["group"]


def _judge_meta(evaluations: list[dict], rows: list[dict]) -> dict:
    """What the judge columns come from: each evaluation, the wordings per group (the P2 report's groups; CR-64 finding 3) and
    the invalid rate per group: invalid results over all results, `sent` rows and exploratory calls left out (CR-17 U4-9)."""
    group_of = {r["parent_run_id"]: r["group"] for r in rows}
    key_of = {r["parent_run_id"]: group_key(r) for r in rows}
    first = evaluations[0]
    wordings: dict[str, set[int]] = {}
    for e in evaluations:
        for ep in e["episodes"]:
            n = len({c["paraphrase"] for c in ep["calls"] if c["backend"] == "llm"})
            if ep["run_id"] in key_of and n:
                wordings.setdefault(key_of[ep["run_id"]], set()).add(n)
    every = {"episodes": [ep for e in evaluations for ep in e["episodes"]]}
    return {
        "evaluations": [
            {"evaluation_id": e["id"], "status": e["status"], "block": e["block"], "episodes": len(e["run_ids"])} for e in evaluations
        ],
        "view": first["view"],
        "transform": BLOCKS[first["block"]].transform,
        "item_set": first["item_set"],
        "backend": "llm",
        "model": first["backends"]["llm"]["model"],
        "model_family": first["backends"]["llm"]["model_family"],
        "wordings_by_group": {g: sorted(n) for g, n in sorted(wordings.items())},
        "invalid_rate_by_group": invalid_rate_by_group(every, group_of),
    }


def _run(db: RunsDB, run_id: str) -> tuple[EpisodeHeader | None, str, list]:
    """(header, status, events). A run with no events yet is planned; one with no run_ended event is still live."""
    events = db.events(run_id)
    header = next((e.header for e in events if e.type == "run_started"), None)
    ended = next((e.status for e in reversed(events) if e.type == "run_ended"), None)
    return header, ended or ("running" if events else "planned"), events


def table(
    db: RunsDB,
    catalog: Catalog,
    research: Research,
    exp_id: str,
    layer: DataLayer,
    evaluations: list[dict] = (),
    trail_of: Callable[[str], list[dict]] | None = None,
) -> tuple[dict, list[str], list[dict]]:
    """(meta, columns, rows), rows in (condition, seed) order. layer A: log-only features; B: every feature. evaluations: the
    details of the evaluations whose LLM scores become the judge columns (check_merge)."""
    evaluations = list(evaluations)
    check_merge(evaluations)
    research.check_feature_code()  # rows from the loaded code must match the hash they carry (CR-26 m1)
    exp = db.experiment(exp_id)
    if exp is None:
        raise ApiError(404, "not_found", f"Experiment {exp_id} not found.")
    doc = json.loads(exp["body_json"])
    cells: dict[tuple[str, int], dict] = {}
    for r in db.experiment_runs(exp_id):
        cell = cells.setdefault((r["condition_id"], r["seed"]), {"parent": None, "children": []})
        if r["kind"] == "parent":
            cell["parent"] = r["run_id"]
        else:
            cell["children"].append((r["kind"], r["idx"], r["run_id"]))
    ids = REGISTRY.ids(layer)
    rows = []
    for (cid, seed), cell in sorted(cells.items()):
        header, status, events = _run(db, cell["parent"])
        spec = RunSpec.model_validate_json(doc["parents"][cell["parent"]]["spec"])  # recorded at create, before any run
        if spec.split == "test":
            research.check_test_open()
        condition = research.condition(cid)
        if condition.hash != spec.condition_hash:
            raise ApiError(409, "conflict", f"Condition {cid} changed after experiment {exp_id} started; its group cannot be read.")
        plans = {
            (p["kind"], p["pos"] * 10 + j): BranchRequest.model_validate(p["request"])
            for p in doc["branches"][cell["parent"]]
            for j in range(p["request"].get("n", 1))
        }
        children = []
        for kind, idx, rid in sorted(cell["children"]):
            child_header, child_status, child_events = _run(db, rid)
            if child_header is not None and child_header.branch is not None:
                br = child_header.branch
                children.append(Child(kind, rid, child_status, br.fork_t, br.intervention, tuple(child_events)))
            else:  # planned, never started: kept, so a feature that needs it says so (CR-23 m1)
                req = plans[(kind, idx)]
                children.append(Child(kind, rid, "planned", req.at, req.intervention, ()))
        labels = spec.labels
        row = {
            "experiment_id": exp_id,
            "condition_id": cid,
            "group": condition.file.group,
            "sub_condition": condition.file.sub_condition,
            "label_legal": labels.legal if labels else None,
            "label_harm": labels.harm if labels else None,
            "split": spec.split,
            "seed": seed,
            "parent_run_id": cell["parent"],
            "parent_status": status,
            "child_run_ids": ";".join(c.run_id for c in children),
            "child_kinds": ";".join(c.kind for c in children),  # same order: kind name, then plan index (0.19.0)
            "config_hash": header.config_hash if header else None,
            "condition_hash": spec.condition_hash,
            "seeds_hash": spec.seeds_hash,
        }
        if header is None:  # the parent never started: ids and labels from the record, null features (CR-23 m2)
            rows.append(row | row_values(ids, (), (), (), None, [], status))
            continue
        scenario = catalog.scenario(header.scenario.id)
        if scenario.hash != header.scenario.hash:
            raise ApiError(409, "conflict", f"Scenario {header.scenario.id} changed after experiment {exp_id} ran.")
        facts = ScenarioFacts(quiet_window=tuple(scenario.quiet_window))
        trail = trail_of(cell["parent"]) if trail_of and status == "completed" and any(REGISTRY[i].access == "trail" for i in ids) else None
        rows.append(row | row_values(ids, header.seller_order, events, children, facts, [], status, trail))
    meta = {"experiment_id": exp_id, "layer": layer, "features_code_hash": REGISTRY.code_hash(), "features": REGISTRY.specs(ids)}
    columns = [*ID_COLUMNS, *feature_columns(ids)]
    if evaluations:
        source = {ep["run_id"]: (e["id"], ep) for e in evaluations for ep in e["episodes"]}
        for i, r in enumerate(rows):
            eid, episode = source.get(r["parent_run_id"], (None, None))
            rows[i] = r | {"judge_evaluation_id": eid} | judge_columns(episode)
        meta["judge"] = _judge_meta(evaluations, rows)
        meta["features"] = [*meta["features"], *_judge_specs(evaluations)]
        columns += ["judge_evaluation_id", *feature_columns(list(JUDGE_FEATURES))]
    return meta, columns, rows


def export_csv(
    db: RunsDB, catalog: Catalog, research: Research, exp_id: str, layer: DataLayer, evaluations: list[dict] = ()
) -> tuple[dict, str]:
    meta, columns, rows = table(db, catalog, research, exp_id, layer, evaluations)
    return meta, to_csv(columns, rows)
