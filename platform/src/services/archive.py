"""Archive and restore runs and experiments (DECISIONS-29). A row of the `archived` table hides a record from GET /runs and
GET /experiments until include_archived=true. Only those two lists read it, so reads by id, exports, features and frozen
research data stay the same, and nothing is deleted."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from adapters.store import RunsDB, Writer
from services.configs import ApiError, Model

Kind = Literal["run", "experiment"]
BLOCKS = "running"  # DECISIONS-29 entry 2: only a running run or experiment blocks; a queued one is archivable


class ArchiveRequest(Model):
    ids: list[str] | None = Field(default=None, min_length=1, description="The records to change")
    all: Literal[True] | None = Field(
        default=None,
        description="Every record on the server, whatever the list filters (D29 entry 2): archive acts on the records that are not"
        " archived, unarchive on the archived ones",
    )

    @model_validator(mode="after")
    def _one(self) -> ArchiveRequest:
        if (self.ids is None) == (self.all is None):
            raise ValueError("give ids or all: true, not both")
        return self


def change(db: RunsDB, writer: Writer, kind: Kind, status: dict[str, str | None], body: ArchiveRequest, archive: bool, now: str) -> dict:
    """{count, skipped}. `status`: every record's id -> its status. count: records changed; skipped: running records that `all`
    left out. With ids, an unknown id is 404 and a running record is 409, and nothing changes."""
    archived = db.archived(kind)
    if body.ids is not None:
        missing = [i for i in body.ids if i not in status]
        if missing:
            raise ApiError(404, "not_found", f"No {kind} {', '.join(missing)}.")
        running = [i for i in body.ids if archive and status[i] == BLOCKS]
        if running:
            raise ApiError(409, "conflict", f"{kind.capitalize()} {', '.join(running)} is running. Stop it or wait, then archive it.")
        targets, skipped = list(dict.fromkeys(body.ids)), []
    else:
        targets = [i for i in status if (i in archived) != archive]
        skipped = [i for i in targets if archive and status[i] == BLOCKS]
        targets = [i for i in targets if i not in skipped]
    targets = [i for i in targets if (i in archived) != archive]

    def write(conn) -> None:
        if archive:
            conn.executemany("INSERT OR IGNORE INTO archived (kind, id, archived_at) VALUES (?, ?, ?)", [(kind, i, now) for i in targets])
        else:
            conn.executemany("DELETE FROM archived WHERE kind = ? AND id = ?", [(kind, i) for i in targets])

    writer.submit(write).result()
    return {"count": len(targets), "skipped": skipped}
