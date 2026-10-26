"""Read models for the API: each is a fold over a run's stored events (state = fold(events)), plus snapshots for seek."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import typing
import zipfile
from pathlib import Path

from adapters.store import RunsDB
from adapters.trail import trail_lines, trail_text
from indicators import Instance
from services.configs import ApiError
from simcore import __version__
from simcore.state import RunState
from simcore.tools import TOOLS
from simschema.episode import episode_lines
from simschema.events import EpisodeHeader, EventBase

MARKER_KIND = {"news_published": "news", "posted": "post", "budget_warning": "budget"}
DIGITS = 6


def indicator_body(inst: Instance) -> dict:
    """The GET /runs/{id}/indicators/{indicator_id} body without run_id. Values are rounded to DIGITS here, not in the indicator."""
    return {
        "indicator_id": inst.indicator_id,
        "version": inst.version,
        "params": inst.params,
        "until": inst.until,
        "t": list(inst.t),
        "outputs": {k: [None if v is None else round(v, DIGITS) for v in vs] for k, vs in inst.outputs.items()},
    }


def summary(row: dict) -> dict:
    config = json.loads(row["config_json"])
    spec = json.loads(row["spec_json"])
    header = EpisodeHeader.model_validate_json(row["header_json"]) if row.get("header_json") else None
    branch = json.loads(row["branch_json"])["branch"] if row.get("branch_json") else None
    agents = spec["agents"]
    models = sorted({a.get("model") for a in agents if a.get("model")})
    return {
        "id": row["run_id"],
        "batch_id": row["batch_id"],
        "experiment_id": row["experiment_id"],
        "kind": spec["kind"],
        "name": config.get("name"),
        "status": row["status"] or "queued",
        "condition_id": spec.get("condition_id"),
        "labels": spec.get("labels"),
        "split": spec["split"],
        "scenario_id": config["scenario_id"],
        "seed": spec["seed"],
        "call_mode": spec["call_mode"],
        "clock_mode": "sync",
        "t": int(row["last_t"] or 0),
        "max_rounds": header.max_rounds if header else 0,
        "branch": branch,
        "cost_usd": round(row["cost_usd"] or 0, 6),
        "calls": row["calls"],
        "cache_hits": row["cache_hits"] or 0,
        "fallbacks": row["fallbacks"],
        "clamps": row["clamps"],
        "created_at": row["created_at"],
        "ended_at": row["ended_at"],
        "end_reason": row["end_reason"],
        "preset_id": spec.get("preset_id"),
        "config_hash": spec.get("config_hash") or "",
        "cloned_from": spec.get("cloned_from"),
        "demand": header.demand if header else "formula",
        "reproducible": header.reproducible if header else True,
        "models": models,
        "source": "import" if any(a.get("policy_kind") == "imported" for a in agents) else "simulator",
    }


def with_hold(item: dict, held: dict[str, str]) -> dict:
    """A run this server holds (it cannot build it, for example paid calls off) is `recovering` with `hold_reason`, also before
    its first event. Added in the routes, not in summary(), so the export's meta.json does not change."""
    code = held.get(item["id"])
    if code and item["status"] in ("queued", "recovering"):
        return item | {"status": "recovering", "hold_reason": code}
    return item | {"hold_reason": None}


def find(db: RunsDB, run_id: str) -> dict:
    for row in db.runs():
        if row["run_id"] == run_id:
            return row
    raise ApiError(404, "not_found", f"Run {run_id} not found.")


def detail(db: RunsDB, run_id: str) -> dict:
    rows = db.runs()
    row = next((r for r in rows if r["run_id"] == run_id), None)
    if row is None:
        raise ApiError(404, "not_found", f"Run {run_id} not found.")
    header = EpisodeHeader.model_validate_json(row["header_json"]) if row.get("header_json") else None
    return summary(row) | {
        "config": json.loads(row["config_json"]),
        "scenario_hash": json.loads(row["config_json"])["scenario_hash"],
        "condition_hash": header.condition_hash if header else None,
        "seeds_hash": header.seeds_hash if header else None,
        "code_version": __version__,
        "snapshots": sorted(db.snapshots(run_id)),
        "children": [r["run_id"] for r in rows if r["parent_run_id"] == run_id],
        "error": row["error"],
        "error_detail": json.loads(row["error_detail"]) if row["error_detail"] else None,
    }


def event_page(db: RunsDB, run_id: str, from_seq: int, limit: int, types: set[str] | None, agent_id: str | None) -> dict:
    events = db.events(run_id)
    last = events[-1].seq if events else -1
    picked = [
        e
        for e in events
        if e.seq >= from_seq and (not types or e.type in types) and (agent_id is None or getattr(e, "agent_id", None) == agent_id)
    ]
    page = picked[:limit]
    more = len(picked) > limit
    return {"events": [e.model_dump(mode="json") for e in page], "last_seq": last, "next_from_seq": page[-1].seq + 1 if more else None}


def series(run_id: str, events: list[EventBase]) -> dict:
    settled = [e for e in events if e.type == "settled"]
    refs = {e.t: e for e in events if e.type == "reference"}
    sellers = sorted(settled[0].prices) if settled else []
    ts = [e.t for e in settled]
    acted = {(e.t, e.agent_id): e for e in events if e.type == "acted"}
    out = {
        "run_id": run_id,
        "t": ts,
        "sellers": {
            s: {
                "price": [e.prices[s] for e in settled],
                "quantity": [e.quantities[s] for e in settled],
                "profit": [e.profits[s] for e in settled],
                "best_response": [refs[t].best_response.get(s) for t in ts],
                "fallback": [bool(acted.get((t, s)) and acted[(t, s)].fallback) for t in ts],
                "forced": [bool(acted.get((t, s)) and acted[(t, s)].forced) for t in ts],
            }
            for s in sellers
        },
        "reference": {"nash": [refs[t].reference.nash for t in ts], "joint_optimum": [refs[t].reference.joint_optimum for t in ts]},
        "consumer_surplus": [refs[t].consumer_surplus for t in ts],
        "markers": [],
    }
    for e in events:
        if e.type in MARKER_KIND:
            out["markers"].append(
                {
                    "t": e.t,
                    "kind": MARKER_KIND[e.type],
                    "seq": e.seq,
                    "agent_id": getattr(e, "agent_id", None),
                    "channel_id": getattr(e, "channel_id", None),
                    "text": getattr(e, "text", None) or getattr(e, "level", ""),
                }
            )
        elif e.type == "intervened":
            out["markers"].append(
                {
                    "t": e.t,
                    "kind": e.intervention.type,
                    "seq": e.seq,
                    "agent_id": getattr(e.intervention, "target", None),
                    "channel_id": getattr(e.intervention, "channel_id", None),
                    "text": e.intervention.type,
                }
            )
        elif e.type == "acted" and e.fallback:
            out["markers"].append(
                {"t": e.t, "kind": "fallback", "seq": e.seq, "agent_id": e.agent_id, "channel_id": None, "text": "fallback"}
            )
    return out


def folded_state(db: RunsDB, run_id: str, t: int | None) -> dict:
    events = db.events(run_id)
    if not events:
        raise ApiError(404, "not_found", f"Run {run_id} not found.")
    snaps = db.snapshots(run_id)
    if t is None:
        t = max(snaps, default=0)
    if t not in snaps:
        raise ApiError(
            404, "snapshot_missing", f"Run {run_id} has no state for round {t} (rounds {min(snaps, default=0)}-{max(snaps, default=0)})."
        )
    snap_hash, blob = snaps[t]
    state = RunState.restore(blob)
    saved = next(e for e in events if e.type == "snapshot_saved" and e.t == t)
    ref = next((e for e in events if e.type == "reference" and e.t == t), None)
    sellers = {}
    for s, hist in state.history.items():
        point = next((p for p in hist if p.t == t), None)
        outcome = next((o for o in state.outcomes.get(s, []) if o.t == t), None)
        sellers[s] = {
            "price": point.price if point else None,
            "quantity": outcome.quantity if outcome else None,
            "profit": outcome.profit if outcome else None,
            "cumulative_profit": sum(o.profit for o in state.outcomes.get(s, []) if o.t <= t),
            "set_by_system": bool(point and point.forced),
        }
    return {
        "run_id": run_id,
        "t": t,
        "last_seq": saved.seq,
        "snapshot_hash": snap_hash,
        "reference": ref.reference.model_dump(mode="json") if ref else None,
        "sellers": sellers,
        "messages": [m.model_dump(mode="json") for m in state.messages],
        "news": [n.model_dump(mode="json") for n in state.news],
        "masks": [m.model_dump(mode="json") for m in state.masks],
    }


def _chat(m: dict) -> dict:
    out = {"role": m["role"], "content": m.get("content")}
    if m.get("tool_calls"):
        out["tool_calls"] = [{"id": c["id"], "name": c["name"], "arguments": json.loads(c["arguments"] or "{}")} for c in m["tool_calls"]]
    if m.get("tool_call_id"):
        out["tool_call_id"] = m["tool_call_id"]
    return out


def observation(db: RunsDB, cache_path: Path, run_id: str, agent_id: str, t: int) -> dict:
    events = [e for e in db.events(run_id) if e.t == t and getattr(e, "agent_id", None) == agent_id]
    observed = [e for e in events if e.type == "observed"]
    if not observed:
        raise ApiError(404, "not_found", f"No observation for {agent_id} at round {t}.")
    obs = next((e for e in observed if e.purpose in ("act", "one_call")), observed[0])
    versions = {s.id: s.version for s in TOOLS.specs()}
    calls = []
    conn = sqlite3.connect(cache_path) if Path(cache_path).exists() else None
    try:
        for e in (e for e in events if e.type == "llm_call"):
            row = conn.execute("SELECT request_json, reply_json FROM calls WHERE key = ?", (e.request_hash,)).fetchone() if conn else None
            call = {"purpose": e.purpose, "request_hash": e.request_hash, "cache_hit": e.cache_hit, "messages": [], "tools": []}
            if row:
                req, reply = json.loads(row[0]), json.loads(row[1])
                call["messages"] = [_chat(m) for m in req["messages"]]
                call["tools"] = [{**t, "tool_id": t["name"], "tool_version": versions.get(t["name"])} for t in req["tools"]]
                call["response"] = _chat({"role": "assistant", "content": reply["text"], "tool_calls": reply["tool_calls"]})
            calls.append(call)
    finally:
        if conn:
            conn.close()
    return {"run_id": run_id, "agent_id": agent_id, "t": t, "observation": obs.observation.model_dump(mode="json"), "calls": calls}


def episode_text(db: RunsDB, run_id: str) -> str:
    events = db.events(run_id)
    if not events:
        raise ApiError(404, "not_found", f"Run {run_id} not found.")
    return "\n".join(episode_lines(events)) + "\n"


MANIFEST_VERSION = "export-manifest.v1"
SPOOL_BYTES = 8 * 1024 * 1024  # a larger zip goes to a temporary file, not memory (CR-66 finding 4)
ZIP_DATE = (1980, 1, 1, 0, 0, 0)  # every entry's date, so two exports of one run are the same bytes (CR-65 note)


def _entry(z: zipfile.ZipFile, path: str, data: bytes) -> None:
    info = zipfile.ZipInfo(path, date_time=ZIP_DATE)
    info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
    z.writestr(info, data)


def run_split(row: dict) -> str:
    return json.loads(row["spec_json"])["split"]


class _Export:
    """A zip that records the sha256 of each file it writes in manifest.json, written last (DECISIONS-14 §2.3)."""

    def __init__(self, z: zipfile.ZipFile, db: RunsDB, runs_dir: Path, cache_path: Path) -> None:
        self.z, self.db, self.runs_dir, self.cache_path, self.files = z, db, Path(runs_dir), cache_path, []

    def write(self, path: str, data: bytes) -> None:
        _entry(self.z, path, data)
        self.files.append(_digest(path, data))

    def run(self, prefix: str, row: dict) -> None:
        for name, data in run_files(self.db, self.runs_dir, self.cache_path, row):
            self.write(prefix + name, data)

    def close(self, **about) -> None:
        manifest = {"manifest": MANIFEST_VERSION, **about, "files": sorted(self.files, key=lambda f: f["path"])}
        _entry(self.z, "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))


def _digest(name: str, data: bytes) -> dict:
    return {"path": name, "bytes": len(data), "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}


def run_files(db: RunsDB, runs_dir: Path, cache_path: Path, row: dict) -> list[tuple[str, bytes]]:
    """episode.jsonl, meta.json and the sidecar trail.jsonl of one run (the one finish_run stored, else built again). The
    run zip, the experiment zip and the run folder all take these bytes (DECISIONS-20 §3)."""
    rid = row["run_id"]
    stored = Path(runs_dir) / rid / "trail.jsonl"
    trail = stored.read_bytes() if stored.is_file() else trail_text(db.events(rid), cache_path).encode("utf-8")
    return [
        ("episode.jsonl", episode_text(db, rid).encode("utf-8")),
        ("meta.json", json.dumps(summary(row), indent=2, sort_keys=True).encode("utf-8")),
        ("trail.jsonl", trail),
    ]


RUN_FOLDER = "run-folder.v1"
EXPERIMENT_FOLDER = "experiment-folder.v1"
_folders = threading.Lock()
"""One writer of folder files at a time: two branches that end together each rewrite their parent's manifest."""


def _put(path: Path, data: bytes) -> None:
    """Write a file whole: a temporary file in the same folder, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _json(doc: dict) -> bytes:
    return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")


def _run_manifest(db: RunsDB, runs_dir: Path, run_id: str, files: list[dict]) -> None:
    folder = Path(runs_dir)
    children = sorted(r["run_id"] for r in db.runs() if r["parent_run_id"] == run_id)
    branches = [{"run_id": c, "path": f"../{c}/"} for c in children if (folder / c / "manifest.json").is_file()]
    _put(folder / run_id / "manifest.json", _json({"manifest": RUN_FOLDER, "run_id": run_id, "files": files, "branches": branches}))


def write_run_folder(db: RunsDB, runs_dir: Path, cache_path: Path, run_id: str) -> None:
    """DECISIONS-20 §1: runs/<run_id>/ holds what the run zip holds (episode.jsonl, meta.json, trail.jsonl) and
    manifest.json (each file's bytes and sha256, and the folder of each ended branch run). A branch also rewrites its
    parent's manifest. Called after finish_run, so the episode, its hash and the ledger line are already final."""
    row = find(db, run_id)
    files = run_files(db, runs_dir, cache_path, row)
    with _folders:
        try:
            (Path(runs_dir) / run_id / "manifest.json").unlink(missing_ok=True)  # a rewrite that fails leaves no stale manifest
            for name, data in files:
                _put(Path(runs_dir) / run_id / name, data)
            _run_manifest(db, runs_dir, run_id, [_digest(name, data) for name, data in files])
        finally:  # the parent lists only children whose folder has a manifest, also after a failed rewrite
            parent = row["parent_run_id"]
            manifest = Path(runs_dir) / str(parent) / "manifest.json"
            if parent and manifest.is_file():
                _run_manifest(db, runs_dir, parent, json.loads(manifest.read_text(encoding="utf-8"))["files"])


def write_experiment_folder(runs_dir: Path, experiments_dir: Path, detail: dict) -> None:
    """DECISIONS-20 §2: experiments/<id>/ holds experiment.json (the GET /experiments/{id} body, as the experiment zip
    has it) and manifest.json (its bytes and sha256, and the folder of each run of the experiment that has one)."""
    exp_id = detail["id"]
    data = _json(detail)
    up = Path(os.path.relpath(runs_dir, Path(experiments_dir) / exp_id)).as_posix()
    planned = [c["parent"] for c in detail["cells"]] + [b for c in detail["cells"] for b in c["branches"]]
    runs = [
        {"run_id": r["run_id"], "path": f"{up}/{r['run_id']}/", "status": r.get("status")}
        for r in sorted(planned, key=lambda r: r["run_id"])
        if (Path(runs_dir) / r["run_id"] / "manifest.json").is_file()
    ]
    manifest = {"manifest": EXPERIMENT_FOLDER, "experiment_id": exp_id, "files": [_digest("experiment.json", data)], "runs": runs}
    with _folders:
        _put(Path(experiments_dir) / exp_id / "experiment.json", data)
        _put(Path(experiments_dir) / exp_id / "manifest.json", _json(manifest))


def _zip(db: RunsDB, runs_dir: Path, cache_path: Path, fill) -> typing.IO[bytes]:
    """The zip in a spooled temporary file, at position 0. The caller closes it."""
    f = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
    try:
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            fill(_Export(z, db, runs_dir, cache_path))
    except BaseException:
        f.close()
        raise
    f.seek(0)
    return f


def export_zip(db: RunsDB, runs_dir: Path, cache_path: Path, run_id: str, with_branches: bool) -> typing.IO[bytes]:
    rows = {r["run_id"]: r for r in db.runs()}
    if run_id not in rows:
        raise ApiError(404, "not_found", f"Run {run_id} not found.")
    ids = [run_id] + ([r for r, row in rows.items() if row["parent_run_id"] == run_id] if with_branches else [])

    def fill(out: _Export) -> None:
        for rid in ids:
            out.run("" if rid == run_id else f"branches/{rid}/", rows[rid])
        out.close(run_id=run_id)

    return _zip(db, runs_dir, cache_path, fill)


def experiment_zip(db: RunsDB, runs_dir: Path, cache_path: Path, detail: dict) -> typing.IO[bytes]:
    """experiment.json (GET /experiments/{id}) and runs/<run_id>/ for each parent and branch run that has started."""
    rows = {r["run_id"]: r for r in db.runs()}
    planned = [c["parent"]["run_id"] for c in detail["cells"]] + [b["run_id"] for c in detail["cells"] for b in c["branches"]]

    def fill(out: _Export) -> None:
        out.write("experiment.json", json.dumps(detail, indent=2, sort_keys=True).encode("utf-8"))
        for rid in (r for r in planned if r in rows):
            out.run(f"runs/{rid}/", rows[rid])
        out.close(experiment_id=detail["id"])

    return _zip(db, runs_dir, cache_path, fill)


def trail_line(db: RunsDB, runs_dir: Path, cache_path: Path, run_id: str, seq: int) -> dict:
    """The trail line of one llm_call: from the stored trail.jsonl, else built from the cache (as the export)."""
    find(db, run_id)
    stored = Path(runs_dir) / run_id / "trail.jsonl"
    if stored.is_file():
        found = [line for line in map(json.loads, stored.read_text(encoding="utf-8").splitlines()) if line["seq"] == seq]
    else:
        found = trail_lines([e for e in db.events(run_id) if e.type == "llm_call" and e.seq == seq], cache_path)
    if not found:
        raise ApiError(404, "not_found", f"Run {run_id} has no llm_call with seq {seq}.")
    return found[0]


def export_csv(run_id: str, events: list[EventBase]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["run_id", "t", "seller", "price", "quantity", "profit", "best_response", "nash", "joint_optimum", "consumer_surplus"])
    refs = {e.t: e for e in events if e.type == "reference"}
    for e in (e for e in events if e.type == "settled"):
        r = refs[e.t]
        for s in sorted(e.prices):
            w.writerow(
                [
                    run_id,
                    e.t,
                    s,
                    e.prices[s],
                    e.quantities[s],
                    e.profits[s],
                    r.best_response.get(s, ""),
                    r.reference.nash_by_seller[s],
                    r.reference.joint_by_seller[s],
                    r.consumer_surplus,
                ]
            )
    return buf.getvalue()


def ledger_entry(row: dict) -> dict:
    keys = (
        "line",
        "run_id",
        "batch_id",
        "kind",
        "status",
        "condition_id",
        "split",
        "seed",
        "config_hash",
        "scenario_hash",
        "episode_hash",
        "cost_usd",
        "created_at",
        "prev_hash",
        "hash",
        "preset_id",
        "cloned_from",
        "demand",
        "reproducible",
        "models",
        "parent_labels",
    )
    out = {k: row.get(k) for k in keys}
    out["config_hash"] = out["config_hash"] or ""
    out["cost_usd"] = out["cost_usd"] or 0.0
    return {
        k: v for k, v in out.items() if v is not None or k in ("condition_id", "episode_hash", "preset_id", "cloned_from", "parent_labels")
    }
