import pytest

from simkit import JOINT, NASH, run
from simschema.episode import EpisodeFormatError, episode_lines, read_episode


def test_episode_jsonl_round_trips_header_and_every_event():
    sink, *_ = run({"A": ("grim_trigger", {"target": JOINT}), "B": ("constant", {"price": NASH})})
    lines = episode_lines(sink.events)
    header, events = read_episode(lines)
    assert header == sink.events[0].header
    assert events == sink.events
    assert len(lines) == len(sink.events) + 1


@pytest.mark.parametrize("cut", [lambda ls: ls[:1] + ls[2:], lambda ls: ls[:-1], lambda ls: [ls[0]] + ls[1:3] + ls[4:]])
def test_a_gap_a_missing_end_or_a_missing_start_is_refused(cut):
    sink, *_ = run({"A": ("constant", {"price": NASH}), "B": ("constant", {"price": NASH})})
    with pytest.raises(EpisodeFormatError):
        read_episode(cut(episode_lines(sink.events)))
