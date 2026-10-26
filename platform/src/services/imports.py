"""POST /imports (QUEUE "Log import", unit 5b, lead 22 Oct): an outside log stored as a completed run.

The run is exploratory (split exploratory, seed 0), its sellers have policy kind `imported`, and its summary says
source import. It holds the adapter's tier-A episode (adapters/log_import.py) and the trail.jsonl beside it, so the
run reads, exports and the judge (block `imported`) work on it as on a simulator run. It has no market state: it cannot
be branched, and features that need the payoff model are missing. The run id comes from the files' hash, so a second
import of the same files returns the stored run (200) and writes nothing. Like a simulator run, it gets its run folder
(runs/<run_id>/ with manifest.json, DECISIONS-20; CR-86 minor 4).
"""

from __future__ import annotations

import logging
import os
import threading
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from adapters.keys import redact
from adapters.log_import import Imported, LogImportError, import_pi, import_rows
from adapters.store import RunsDB, StoreSink, Writer, finish_run, put_run_config
from services.configs import AgentConfig, ApiError, BudgetSettings, ChannelSettings, ClockSettings, Model, PolicySettings, RunConfig
from services.read import write_run_folder
from simcore.run import RunSpec
from simschema.episode import read_episode

IMPORTED = "imported"
MAX_CHARS = 20_000_000
_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()

log = logging.getLogger("sim.imports")


def is_imported(config: RunConfig) -> bool:
    return any(a.policy.kind == IMPORTED for a in config.agents)


class ImportCreate(Model):
    format: Literal["pi", "rows"] = Field(description="pi: one Pi-agent session export per seller; rows: one JSON or CSV file")
    files: dict[str, str] = Field(
        min_length=1,
        description="The files' text. pi: seller id -> that seller's session export (.jsonl). rows: file name -> its text (one"
        " file; a name ending in .csv is read as CSV, any other as JSON)",
    )
    name: str | None = Field(None, max_length=200)
    created_at: str | None = Field(None, description="rows only: the run's created_at; null: the time of the import")
    model_families: dict[str, str] = Field(
        default_factory=dict,
        description="Seller id -> the model family of the log's model. The judge refuses a run whose seller has none, as for"
        " LLM runs (judge_same_family)",
    )

    @model_validator(mode="after")
    def _shape(self) -> "ImportCreate":
        if sum(len(t) for t in self.files.values()) > MAX_CHARS:
            raise ValueError(f"the files hold more than {MAX_CHARS} characters")
        if self.format == "rows" and len(self.files) != 1:
            raise ValueError("format rows takes one file")
        if self.format == "pi" and len(self.files) < 2:
            raise ValueError("format pi takes one session per seller, at least two")
        return self


def _parse(body: ImportCreate, now: str) -> Imported:
    try:
        if body.format == "pi":
            return import_pi({a: t.encode("utf-8") for a, t in body.files.items()})
        ((name, text),) = body.files.items()
        return import_rows(name, text.encode("utf-8"), body.created_at or now)
    except LogImportError as e:
        raise ApiError(422, "validation_error", f"The log cannot be imported: {e}", [{"path": "files", "message": str(e)}]) from None


def _config(body: ImportCreate, imported: Imported) -> RunConfig:
    header = imported.header
    kinds = {c.kind for c in header.channels}
    unknown = sorted(set(body.model_families) - {a.id for a in header.agents})
    if unknown:
        raise ApiError(422, "validation_error", f"model_families names {unknown}, who are not speakers of the log.",
                       [{"path": "model_families", "message": "not a speaker"}])  # fmt: skip
    agents = [
        AgentConfig(
            id=a.id,
            role="seller",
            policy=PolicySettings(
                kind=IMPORTED, params={"source": body.format, "model": a.model, "model_family": body.model_families.get(a.id)}
            ),
        )
        for a in header.agents
    ]
    return RunConfig(
        kind="exploratory",
        name=body.name or f"import ({body.format})",
        scenario_id=header.scenario.id,
        scenario_hash=header.scenario.hash,
        seeds=[0],
        agents=agents,
        channels=ChannelSettings(public="public" in kinds, private_pairs="private" in kinds, news=False),
        call_mode=header.call_mode,
        clock=ClockSettings(mode="sync"),
        budget=BudgetSettings(max_calls=1),
        notes=f"Imported log ({body.format}); no market state, so no branch and no model features.",
    )


def _lock(run_id: str) -> threading.Lock:
    with _locks_lock:
        return _locks.setdefault(run_id, threading.Lock())


def create(db: RunsDB, writer: Writer, runs_dir: Path, body: ImportCreate, now: str, cache_path: Path | None = None) -> tuple[bool, dict]:
    """(created, body). Stores the run once; the same files again return it with created false.

    A run is stored when its ledger line exists (CR-76 minor 1). One lock per run id makes two imports of the same files
    store it once. An import that stopped before its ledger line is stored again: the config and event inserts ignore
    rows that exist, the trail is written whole, and finish_run then writes the one ledger line. Then the run folder is
    written (CR-86 minor 4), also by a later import of the same files when the folder has no manifest yet. `cache_path`:
    the call cache, which the folder does not read for an import (its trail.jsonl is stored)."""
    imported = _parse(body, now)
    header, events = read_episode(imported.lines)
    out = {
        "run_id": imported.run_id,
        "batch_id": "batch_" + imported.run_id.removeprefix("run_"),
        "events": dict(Counter(e.type for e in events)),
        "trail_lines": len(imported.trail),
    }
    with _lock(imported.run_id):
        created = not db.ledger(imported.run_id)
        if created:
            _store(db, writer, runs_dir, body, imported, out["batch_id"], events)
        if created or not (Path(runs_dir) / imported.run_id / "manifest.json").is_file():
            _folder(db, runs_dir, cache_path, imported.run_id)
    return created, out | {"created": created}


def _folder(db: RunsDB, runs_dir: Path, cache_path: Path | None, run_id: str) -> None:
    """As RunService._folder: a copy of what the databases hold, so a failure is logged and never fails the import."""
    try:
        write_run_folder(db, runs_dir, cache_path, run_id)
    except Exception as e:  # noqa: BLE001
        log.error("run folder of %s not written: %s: %s", run_id, type(e).__name__, redact(str(e)))


def _store(db: RunsDB, writer: Writer, runs_dir: Path, body: ImportCreate, imported: Imported, batch_id: str, events: list) -> None:
    header = imported.header
    config = _config(body, imported)
    spec = RunSpec(
        run_id=imported.run_id,
        batch_id=batch_id,
        kind="exploratory",
        split="exploratory",
        seed=0,
        agents=header.agents,
        public=config.channels.public,
        private_pairs=config.channels.private_pairs,
        call_mode=header.call_mode,
        created_at=header.created_at,
    )
    put_run_config(
        writer,
        spec.run_id,
        batch_id,
        None,
        config.model_dump_json(exclude_none=True),
        spec.model_dump_json(),
        None,
        header.created_at,
    )
    StoreSink(writer).write(events, {})
    folder = Path(runs_dir) / spec.run_id
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "trail.jsonl.tmp"
    tmp.write_bytes(imported.trail_text().encode("utf-8"))
    os.replace(tmp, folder / "trail.jsonl")
    finish_run(db, writer, spec.run_id, runs_dir)
