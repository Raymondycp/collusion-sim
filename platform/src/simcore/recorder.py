"""Recorder: stamps seq, run_id, wall time and tier on each event, keeps the episode hash, fans out to sinks."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol, TypeVar

from simcore.state import RunState
from simschema.events import TIER, EventBase, SnapshotSavedEvent

E = TypeVar("E", bound=EventBase)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventSink(Protocol):
    def event(self, e: EventBase) -> None: ...

    def snapshot(self, run_id: str, t: int, snapshot_hash: str, blob: str) -> None: ...


@dataclass
class MemorySink:
    events: list[EventBase] = field(default_factory=list)
    snapshots: dict[int, tuple[str, str]] = field(default_factory=dict)

    def event(self, e: EventBase) -> None:
        self.events.append(e)

    def snapshot(self, run_id: str, t: int, snapshot_hash: str, blob: str) -> None:
        self.snapshots[t] = (snapshot_hash, blob)


# Fields added after episodes were stored, by event type, as dotted paths. A null one is left out of the hash line,
# so an episode stored before the field existed re-hashes to its stored hash (CR-50 addendum; contracts 0.18.0, 0.21.0,
# 0.22.1, 0.23.0; DECISIONS-15 §1 and §3).
LATER_FIELDS: dict[str, tuple[str, ...]] = {
    "observed": (
        "observation.rival_last_prices",
        "observation.new_messages",
        "observation.news",
        "observation.recent_messages",
        "observation.own_history",
        "observation.mask_noop",
        "observation.rival_history",
    ),
    "posted": ("receivers",),
    "run_started": ("header.observation_mode",),
    "planned": ("source",),
    "llm_call": ("http_status", "provider_code", "degraded", "slot_wait_ms", "first_byte_ms", "parallel", "provider_message", "settings"),
    "talk_replied": ("reason",),
    "acted": ("fallback_reason", "asked_price"),
    "run_ended": ("error_detail",),
}


def _hash_line(e: EventBase) -> str:
    """The episode hash covers every field except wall_time, so a rerun with the same seed and cache gives the same hash.
    A LATER_FIELDS field that is null is left out."""
    drop: dict = {"wall_time": True}
    for path in LATER_FIELDS.get(e.type, ()):
        *parents, name = path.split(".")
        obj, node = e, drop
        for p in parents:
            obj, node = getattr(obj, p), node.setdefault(p, {})
        if getattr(obj, name) is None:
            node[name] = True
    return e.model_dump_json(exclude=drop) + "\n"


class Recorder:
    def __init__(self, run_id: str, sinks: list[EventSink], now: Callable[[], str] = utc_now, prior: Sequence[EventBase] = ()) -> None:
        """`prior`: events already stored for this run (resume). seq and the rolling hash continue after them."""
        self.run_id = run_id
        self.sinks = sinks
        self.now = now
        self.seq = len(prior)
        self.guard: Callable[[str], None] | None = None
        self._hash = hashlib.sha256()
        self._local = threading.local()
        self._lock = threading.Lock()
        self.deferred: list[EventBase] = []
        for e in prior:
            self._hash.update(_hash_line(e).encode("utf-8"))

    def emit(self, cls: type[E], t: float, **fields) -> E:
        kind = cls.model_fields["type"].default
        if self.guard:
            self.guard(kind)
        buffer = getattr(self._local, "buffer", None)
        if buffer is not None:  # a parallel phase: seq is given when the buffer is merged (DECISIONS-22 entry 5.6)
            e = cls(seq=0, run_id=self.run_id, t=t, wall_time=self.now(), tier=TIER[kind], **fields)
            if kind == "budget_warning":
                with self._lock:
                    self.deferred.append(e)
            else:
                buffer.append(e)
            return e
        return self._commit(cls(seq=self.seq, run_id=self.run_id, t=t, wall_time=self.now(), tier=TIER[kind], **fields))

    def _commit(self, e: E) -> E:
        self._hash.update(_hash_line(e).encode("utf-8"))
        self.seq += 1
        for sink in self.sinks:
            sink.event(e)
        return e

    @contextmanager
    def buffered(self) -> Iterator[list[EventBase]]:
        """Events this thread emits go to a list, not to the sinks; budget warnings go to `deferred` (D22 entry 5.6)."""
        self._local.buffer = buffer = []
        try:
            yield buffer
        finally:
            self._local.buffer = None

    def merge(self, events: list[EventBase]) -> None:
        """Record buffered events in their order: seq and the hash are given now; the wall time stays the time of emit."""
        for e in events:
            if self.guard:
                self.guard(e.type)
            self._commit(e.model_copy(update={"seq": self.seq}))

    def snapshot(self, t: int, state: RunState) -> SnapshotSavedEvent:
        snapshot_hash, blob = state.snapshot()
        for sink in self.sinks:
            sink.snapshot(self.run_id, t, snapshot_hash, blob)
        return self.emit(SnapshotSavedEvent, t, snapshot_hash=snapshot_hash)

    @property
    def episode_hash(self) -> str:
        return "sha256:" + self._hash.copy().hexdigest()
