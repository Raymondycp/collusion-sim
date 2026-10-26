"""Offline episode commands: simschema only, read only (CLI-DESIGN §3.8)."""

import json

import pytest
from conftest import ROOT, invoke

EXAMPLES_DIR = ROOT / "docs" / "sim" / "api" / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("episode-*.jsonl"))  # trail-*.jsonl are trail lines (tests/test_trail_route.py)


def test_each_example_is_an_episode_or_a_trail():
    names = [p.name for p in EXAMPLES_DIR.glob("*.jsonl")]
    assert EXAMPLES and all(n.startswith(("episode-", "trail-")) for n in names), names


def offline(*args):
    return invoke(None, *args)


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.name for p in EXAMPLES])
def test_examples_validate_and_summarise(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    v = offline("episode", "validate", str(path), "--json")
    assert v.exit_code == 0 and v.json["ok"] and v.json["events"] == len(lines) - 1 and v.json["last_seq"] == len(lines) - 2
    s = offline("episode", "summary", str(path), "--json").json
    assert sum(s["by_type"].values()) == v.json["events"] == sum(s["by_tier"].values())
    assert s["rounds"] == s["by_type"]["round_started"] and s["agents"] == ["A", "B"]


def broken(tmp_path, edit) -> str:
    lines = EXAMPLES[0].read_text(encoding="utf-8").splitlines()
    out = tmp_path / "bad.jsonl"
    out.write_text("\n".join(edit(lines)) + "\n", encoding="utf-8")
    return str(out)


def test_structure_errors_are_episode_invalid(tmp_path):
    gap = offline("episode", "validate", broken(tmp_path, lambda ls: ls[:5] + ls[6:]), "--json")
    assert gap.exit_code == 1 and gap.json["code"] == "episode_invalid" and "seq" in gap.json["message"]
    no_end = offline("episode", "validate", broken(tmp_path, lambda ls: ls[:-1]), "--json")
    assert no_end.exit_code == 1 and "run_ended" in no_end.json["message"]


def test_a_bad_event_line_is_located(tmp_path):
    def corrupt(ls):
        event = json.loads(ls[7])
        event["seq"] = "seven"
        return ls[:7] + [json.dumps(event)] + ls[8:]

    r = offline("episode", "validate", broken(tmp_path, corrupt), "--json")
    assert r.exit_code == 1 and r.json["code"] == "episode_invalid" and r.json["errors"][0]["path"] == "line 8"


def test_empty_and_missing_files(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert offline("episode", "validate", str(empty), "--json").json["code"] == "episode_invalid"
    assert offline("episode", "summary", str(tmp_path / "none.jsonl"), "--json").json["code"] == "file_invalid"


def test_offline_commands_write_nothing(tmp_path):
    copy = tmp_path / "ep.jsonl"
    copy.write_bytes(EXAMPLES[0].read_bytes())
    before = sorted(p.name for p in tmp_path.iterdir())
    assert offline("episode", "validate", str(copy)).exit_code == 0
    assert offline("episode", "summary", str(copy)).exit_code == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == before
