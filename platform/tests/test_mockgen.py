import re

from mockgen.__main__ import generate
from simschema.episode import read_episode


def test_mock_episodes_are_valid_deterministic_and_linked(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    generate(first)
    generate(second)
    names = sorted(p.name for p in first.iterdir())
    assert names == ["README.md", "episode-demo-probe.jsonl", "episode-demo.jsonl"]
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()

    parent_header, parent = read_episode((first / "episode-demo.jsonl").read_text(encoding="utf-8").splitlines())
    child_header, child = read_episode((first / "episode-demo-probe.jsonl").read_text(encoding="utf-8").splitlines())
    assert parent_header.branch is None and child_header.branch.parent_run_id == parent_header.run_id
    for h in (parent_header, child_header):
        assert re.fullmatch(r"run_[0-9A-HJKMNP-TV-Z]{26}", h.run_id) and re.fullmatch(r"batch_[0-9A-HJKMNP-TV-Z]{26}", h.batch_id)
    kinds = {e.type for e in parent}
    assert {"news_published", "posted", "acted", "settled", "snapshot_saved", "run_ended"} <= kinds
    assert "intervened" in {e.type for e in child}
    assert [e.seq for e in child] == list(range(len(child)))
    assert max(e.t for e in parent if e.type == "settled") >= 30
