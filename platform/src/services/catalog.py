"""Scenarios: drafts in SQLite (editable), frozen ones as files under scenarios/<id>.json (read-only, committed with the freeze).

Runs read only frozen scenarios. Freezing runs the check, writes the file and removes the draft row.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from services.configs import ApiError
from simcore.scenario import Scenario, ScenarioHashError

DRAFT_FIELDS = {"name", "description", "market", "n_sellers", "seller_ids", "horizon", "quiet_window", "events", "provenance"}


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def experiments(self) -> Path:
        """One folder per experiment (DECISIONS-20 §2)."""
        return self.root / "experiments"

    @property
    def scenarios(self) -> Path:
        return self.root / "scenarios"

    @property
    def conditions(self) -> Path:
        return self.root / "conditions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_draft(scenario_id: str, body: dict) -> Scenario:
    extra = sorted(set(body) - DRAFT_FIELDS)
    if extra:
        raise ApiError(
            422, "validation_error", "A draft body has only draft fields.", [{"path": k, "message": "not a draft field"} for k in extra]
        )
    try:
        return Scenario.model_validate({**body, "id": scenario_id})
    except ValidationError as e:
        raise ApiError(
            422,
            "validation_error",
            "The draft does not match the scenario schema.",
            [{"path": "/".join(str(x) for x in err["loc"]), "message": err["msg"]} for err in e.errors()],
        ) from None


SCENARIO_ID = re.compile(r"scn_[A-Za-z0-9_]+")


class Catalog:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        paths.data.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(paths.data / "scenarios.sqlite", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, version INTEGER NOT NULL, json TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    def close(self) -> None:
        self._conn.close()

    def _file(self, scenario_id: str) -> Path:
        """The frozen file. The id pattern keeps the path inside scenarios/ (no separator, no "..")."""
        if not SCENARIO_ID.fullmatch(scenario_id):
            raise ApiError(404, "not_found", f"Scenario {scenario_id} not found.")
        return self.paths.scenarios / f"{scenario_id}.json"

    def _draft_row(self, scenario_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM drafts WHERE id = ?", (scenario_id,)).fetchone()

    def scenario(self, scenario_id: str) -> Scenario:
        """A frozen scenario (what a run reads). 409 for a draft, 404 for an unknown id."""
        path = self._file(scenario_id)
        if not path.exists():
            if self._draft_row(scenario_id):
                raise ApiError(409, "scenario_not_frozen", f"Scenario {scenario_id} is a draft. Check, approve and freeze it first.")
            raise ApiError(404, "not_found", f"Scenario {scenario_id} not found.")
        try:
            return Scenario.load(path)
        except ScenarioHashError as e:
            raise ApiError(409, "hash_mismatch", str(e)) from None

    def any(self, scenario_id: str) -> Scenario:
        row = self._draft_row(scenario_id)
        return Scenario.model_validate_json(row["json"]) if row else self.scenario(scenario_id)

    def record(self, scenario_id: str) -> dict:
        """Scenario plus the store fields the contract adds (a frozen file is version 1)."""
        row = self._draft_row(scenario_id)
        if row:
            return json.loads(row["json"]) | {"version": row["version"], "created_at": row["created_at"], "updated_at": row["updated_at"]}
        s = self.scenario(scenario_id)
        updated = _mtime(self._file(scenario_id))
        return s.model_dump(mode="json") | {"version": 1, "created_at": s.frozen_at or updated, "updated_at": updated}

    def summaries(self) -> list[dict]:
        out = []
        for path in sorted(self.paths.scenarios.glob("*.json")) if self.paths.scenarios.exists() else []:
            try:
                s = Scenario.load(path)
            except (ScenarioHashError, ValueError):
                continue
            out.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "status": s.status,
                    "hash": s.hash,
                    "n_sellers": s.n_sellers,
                    "n_events": len(s.events),
                    "updated_at": _mtime(path),
                }
            )
        with self._lock:
            rows = self._conn.execute("SELECT * FROM drafts ORDER BY updated_at DESC").fetchall()
        for row in rows:
            s = Scenario.model_validate_json(row["json"])
            out.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "status": "draft",
                    "hash": None,
                    "n_sellers": s.n_sellers,
                    "n_events": len(s.events),
                    "updated_at": row["updated_at"],
                }
            )
        return out

    def create(self, scenario_id: str, body: dict) -> dict:
        draft = parse_draft(scenario_id, body)
        now = _now()
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO drafts VALUES (?, 1, ?, ?, ?)", (scenario_id, draft.model_dump_json(), now, now))
        return self.record(scenario_id)

    def replace(self, scenario_id: str, body: dict) -> dict:
        if self._file(scenario_id).exists():
            raise ApiError(409, "scenario_frozen", f"{scenario_id} is frozen. Copy it to a new draft to change it.")
        if not self._draft_row(scenario_id):
            raise ApiError(404, "not_found", f"Scenario {scenario_id} not found.")
        draft = parse_draft(scenario_id, body)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE drafts SET json = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (draft.model_dump_json(), _now(), scenario_id),
            )
        return self.record(scenario_id)

    def delete(self, scenario_id: str) -> None:
        if self._file(scenario_id).exists():
            raise ApiError(409, "scenario_frozen", f"{scenario_id} is frozen and cannot be deleted.")
        with self._lock, self._conn:
            gone = self._conn.execute("DELETE FROM drafts WHERE id = ?", (scenario_id,)).rowcount
        if not gone:
            raise ApiError(404, "not_found", f"Scenario {scenario_id} not found.")

    def freeze(self, scenario_id: str, approved_by: str) -> dict:
        if self._file(scenario_id).exists():
            raise ApiError(409, "scenario_frozen", f"{scenario_id} is already frozen.")
        row = self._draft_row(scenario_id)
        if not row:
            raise ApiError(404, "not_found", f"Scenario {scenario_id} not found.")
        draft = Scenario.model_validate_json(row["json"])
        report = draft.check()
        if not report.ok:
            raise ApiError(
                422,
                "scenario_check_failed",
                "The scenario check fails; fix the draft first.",
                [{"path": f"checks/{c.id}", "message": c.message} for c in report.checks if not c.ok],
            )
        frozen = draft.frozen(approved_by=approved_by)
        self.paths.scenarios.mkdir(parents=True, exist_ok=True)
        tmp = self._file(scenario_id).with_suffix(".json.tmp")
        frozen.write(tmp)
        tmp.replace(self._file(scenario_id))
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM drafts WHERE id = ?", (scenario_id,))
        return self.record(scenario_id)
