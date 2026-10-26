"""episode.jsonl: line 1 is the EpisodeHeader, each other line one SimEvent (seq 0 = run_started … last = run_ended)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from simschema.events import SIM_EVENT, EpisodeHeader, EventBase


class EpisodeFormatError(ValueError):
    pass


def episode_lines(events: Sequence[EventBase]) -> list[str]:
    if not events or events[0].type != "run_started":
        raise EpisodeFormatError("an episode starts with run_started")
    return [events[0].header.model_dump_json()] + [e.model_dump_json() for e in events]


def read_episode(lines: Iterable[str]) -> tuple[EpisodeHeader, list[EventBase]]:
    it = iter(lines)
    header = EpisodeHeader.model_validate_json(next(it))
    events = [SIM_EVENT.validate_json(line) for line in it if line.strip()]
    if [e.seq for e in events] != list(range(len(events))):
        raise EpisodeFormatError("seq must run 0, 1, 2 … with no gap")
    if not events or events[0].type != "run_started" or events[0].header != header:
        raise EpisodeFormatError("seq 0 must be run_started with the same header as line 1")
    if events[-1].type != "run_ended":
        raise EpisodeFormatError("the last event must be run_ended")
    return header, events
