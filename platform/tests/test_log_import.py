"""Log import (QUEUE "Log import"; DECISIONS-14 §3.3): outside logs to episode.v5 with tier-A events and a trail.jsonl.
Round trips: a simulator episode written out as rows and imported again gives the same log features; the pilot-1
episodes (git-ignored run output, read from the main worktree when present) the same."""

import csv
import json
import subprocess
from pathlib import Path

import pytest

from adapters.log_import import LogImportError, build, feature_row, from_rows, import_pi, import_rows
from features import REGISTRY
from features.table import compute
from judge.views import render
from simkit import capability, run
from simschema.episode import read_episode
from simschema.events import SIM_EVENT, TIER

LOG_FEATURES = [i for i in REGISTRY.ids() if REGISTRY[i].access == "log"]
FRAME = {"run_started", "run_ended"}


def rows_of(events) -> list[dict]:
    """What a log of the episode shows: who said what in which round and channel, each price, quantity and profit."""
    rows = []
    for e in events:
        if e.type == "posted":
            rows.append({"speaker": e.agent_id, "round": int(e.t), "text": e.text, "channel": e.channel_id})
        elif e.type == "settled":
            rows += [
                {"speaker": s, "round": int(e.t), "action": p, "quantity": e.quantities.get(s), "profit": e.profits.get(s)}
                for s, p in e.prices.items()
            ]
    return rows


def log_features(header, events) -> dict:
    return {i: compute(REGISTRY[i], header.seller_order, events, [], None, []) for i in LOG_FEATURES}


def talker_episode():
    caps = {s: capability(channels=("private_A_B",)) for s in "AB"}
    sink, *_ = run(
        {s: ("scripted_talker", {"price": 10.5, "offer_text": f"{s} says hold 10.5", "channel_id": "private_A_B"}) for s in "AB"},
        caps=caps,
        private_pairs=True,
    )
    return sink.events


def follower_episode():
    """Prices that move: A follows a schedule, B copies A's last price, so the price correlations have values."""
    schedule = {"prices": {str(r): p for r, p in zip(range(2, 13), (7, 9, 6, 10, 8, 7, 9.5, 6.5, 10, 8.5, 7.5))}, "default": 10.5}
    sink, *_ = run({"A": ("price_schedule", schedule), "B": ("tit_for_tat", {"start": 10.5})})
    return sink.events


def pilot_files() -> list[Path]:
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True, check=False).stdout.strip()
    roots = {Path.cwd(), Path(common).resolve().parent} if common else {Path.cwd()}
    return sorted({p for r in roots for p in (r / "docs/sim/review/pilot-1").glob("**/events-run_*.json")}, key=lambda p: p.name)


def check_episode(imported) -> list:
    header, events = read_episode(imported.lines)
    assert header.schema_version == "episode.v5" and header.kind == "exploratory" and header.split == "exploratory"
    assert {a.policy_kind for a in header.agents} == {"imported"}
    assert all(TIER[e.type] == "A" for e in events if e.type not in FRAME), "content events are tier A only"
    return events


# Round trips


@pytest.mark.parametrize("make, real", [(talker_episode, "msg_volume"), (follower_episode, "corr_lag")])
def test_a_simulator_episode_written_as_rows_imports_with_the_same_log_features(tmp_path, make, real):
    events = make()
    original = log_features(events[0].header, events)
    log = tmp_path / "log.json"
    log.write_text(json.dumps(rows_of(events)), encoding="utf-8")
    imported = import_rows(log.name, log.read_bytes(), "2026-10-22T00:00:00Z")
    got = check_episode(imported)
    assert log_features(imported.header, got) == original
    assert [(e.t, e.agent_id, e.text) for e in got if e.type == "posted"] == [
        (e.t, e.agent_id, e.text) for e in events if e.type == "posted"
    ]
    assert original[real][0] is not None, "the round trip compares a real value"


@pytest.mark.parametrize("path", pilot_files(), ids=lambda p: p.parent.name)
def test_a_pilot_1_episode_imports_with_the_same_log_features(path, tmp_path):
    events = [SIM_EVENT.validate_python(e) for e in json.loads(path.read_text(encoding="utf-8"))["events"]]
    log = tmp_path / "log.csv"
    with log.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ["speaker", "round", "text", "channel", "action", "quantity", "profit"])
        w.writeheader()
        w.writerows(rows_of(events))
    imported = import_rows(log.name, log.read_bytes(), events[0].header.created_at)
    got = check_episode(imported)
    assert log_features(imported.header, got) == log_features(events[0].header, events)
    assert sum(e.type == "posted" for e in got) == sum(e.type == "posted" for e in events)


def test_the_pilot_1_episodes_are_found_here_or_in_the_main_worktree():
    if not pilot_files():
        pytest.skip("no pilot-1 run output on this machine (git-ignored)")
    assert all(p.name.startswith("events-run_") for p in pilot_files())


# Pi-agent session export


def pi_session(agent: str, rounds: list[tuple[str | None, float]], model="deepseek-v4-flash") -> str:
    lines = [
        {"type": "session", "version": 3, "id": f"s{agent}", "timestamp": "2026-10-22T01:00:00.000Z", "cwd": "/x"},
        {"type": "model_change", "id": "m", "parentId": None, "timestamp": "2026-10-22T01:00:00.000Z", "provider": "p", "modelId": model},
    ]
    for t, (said, price) in enumerate(rounds, start=1):
        at = f"2026-10-22T01:{t:02d}:00.000Z"
        lines.append(
            {
                "type": "message",
                "timestamp": at,
                "message": {"role": "user", "content": [{"type": "text", "text": f"Round {t}. Set your price."}]},
            }
        )
        content = [{"type": "thinking", "thinking": f"{agent} thinks in round {t}", "thinkingSignature": "x"}]
        if said:
            content.append({"type": "toolCall", "id": f"p{t}", "name": "post", "arguments": {"channel_id": "private_A_B", "text": said}})
        usage = {"input": 100, "output": 20, "cacheRead": 0, "reasoning": 5, "cost": {"total": 0.001}}
        lines.append(
            {
                "type": "message",
                "timestamp": at,
                "message": {"role": "assistant", "content": content, "model": model, "usage": usage, "stopReason": "toolUse"},
            }
        )
        lines.append(
            {
                "type": "message",
                "timestamp": at,
                "message": {
                    "role": "toolResult",
                    "toolCallId": f"p{t}",
                    "toolName": "post",
                    "content": [{"type": "text", "text": "posted"}],
                },
            }
        )
        content = [
            {"type": "text", "text": "Done."},
            {"type": "toolCall", "id": f"s{t}", "name": "set_price", "arguments": {"price": price}},
        ]
        lines.append(
            {
                "type": "message",
                "timestamp": at,
                "message": {"role": "assistant", "content": content, "model": model, "usage": usage, "stopReason": "toolUse"},
            }
        )
    return "".join(json.dumps(x) + "\n" for x in lines)


@pytest.fixture
def sessions(tmp_path):
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text(pi_session("A", [("Hold 10.5?", 10.5), (None, 10.5), ("Keep it.", 10.0)]), encoding="utf-8")
    b.write_text(pi_session("B", [("Yes.", 10.5), (None, 9.0), (None, 9.0)], model="other-model"), encoding="utf-8")
    return {"A": a.read_bytes(), "B": b.read_bytes()}


def test_a_pi_session_per_seller_gives_posts_prices_and_a_full_trail(sessions):
    imported = import_pi(sessions)
    events = check_episode(imported)
    assert [(e.t, e.agent_id, e.text, e.receivers) for e in events if e.type == "posted"] == [
        (1, "A", "Hold 10.5?", ["B"]),
        (1, "B", "Yes.", ["A"]),
        (3, "A", "Keep it.", []),
    ]
    assert [e.prices for e in events if e.type == "settled"] == [{"A": 10.5, "B": 10.5}, {"A": 10.5, "B": 9.0}, {"A": 10.0, "B": 9.0}]
    assert [e.quantities for e in events if e.type == "settled"] == [{}] * 3, "a log without quantities gives none"
    assert {a.id: a.model for a in imported.header.agents} == {"A": "deepseek-v4-flash", "B": "other-model"}
    assert imported.header.channels[0].id == "private_A_B" and imported.header.call_mode == "talk_and_act"
    assert len(imported.trail) == 12 and all(line["trail"] == "trail.v1" and line["source"] == "pi" for line in imported.trail)
    first = imported.trail[0]
    assert (first["t"], first["agent_id"], first["seq"], first["input_tokens"], first["cost_usd"]) == (1.0, "A", None, 100, 0.001)
    reply = first["messages"][-1]
    assert [p["part_kind"] for p in reply["parts"]] == ["thinking", "tool-call"] and reply["parts"][0]["content"] == "A thinks in round 1"
    assert first["messages"][0]["parts"][0]["content"] == "Round 1. Set your price."
    assert imported.trail[1]["messages"][0]["parts"][0]["part_kind"] == "tool-return", "the next call sends the tool result"
    assert not [e for e in events if "thinks in round" in e.model_dump_json()], "thinking stays out of the episode"


def test_the_same_files_give_the_same_run_id_and_bytes(sessions):
    one, two = import_pi(sessions), import_pi(sessions)
    assert one.run_id == two.run_id and one.episode_text() == two.episode_text() and one.trail_text() == two.trail_text()


def test_the_judge_view_and_the_feature_row_run_on_an_imported_episode(sessions):
    imported = import_pi(sessions)
    _, events = read_episode(imported.lines)
    text = render("talk_only", imported.run_id, events, []).text
    assert "Hold 10.5?" in text and "Keep it." in text and "private_A_B" not in text
    row = feature_row(imported)
    for i in REGISTRY.ids():
        access = REGISTRY[i].access
        if access == "log":
            assert row[f"{i}_missing"] is None or not row[f"{i}_missing"].startswith("access_"), i
        else:
            assert row[i] is None and row[f"{i}_missing"] == f"access_{access}", i


# Bad input


@pytest.mark.parametrize(
    "rows, message",
    [
        ([{"speaker": "A B", "round": 1}], "is not an agent id"),
        ([{"speaker": "A"}], "speaker and round are required"),
        ([{"speaker": "A", "round": "x"}], "is not a whole number"),
        ([{"speaker": "A", "round": 0}], r"round 0 is not in 1\.\.1000"),
        ([{"speaker": "A", "round": 1001}], r"round 1001 is not in 1\.\.1000"),
        ([{"speaker": "A", "round": 1, "action": "up"}], "is not a number"),
        ([{"speaker": "A", "round": 1, "action": "nan"}], "not a finite number of 0 or more"),
        ([{"speaker": "A", "round": 1, "action": -1}], "not a finite number of 0 or more"),
        ([{"speaker": "A", "round": 1, "action": 5, "quantity": "inf"}], "not a finite number of 0 or more"),
        ([{"speaker": "A", "round": 1, "action": 5, "quantity": -2}], "not a finite number of 0 or more"),
        ([{"speaker": "A", "round": 1, "action": 5, "profit": "nan"}], "profit 'nan' is not a finite number"),
    ],
)
def test_bad_rows_are_refused_with_the_row(rows, message):
    with pytest.raises(LogImportError, match=message):
        from_rows(rows, "2026-10-22T00:00:00Z")


def test_one_speaker_or_an_unknown_channel_is_refused():
    with pytest.raises(LogImportError, match="at least two speakers"):
        build(from_rows([{"speaker": "A", "round": 1, "action": 5}], "2026-10-22T00:00:00Z"), [b"x"], "2026-10-22T00:00:00Z")
    rows = [{"speaker": "A", "round": 1, "text": "hi", "channel": "private_A_C"}, {"speaker": "B", "round": 1, "action": 5}]
    with pytest.raises(LogImportError, match="private_<A>_<B> with two speakers"):
        build(from_rows(rows, "2026-10-22T00:00:00Z"), [b"x"], "2026-10-22T00:00:00Z")


def test_a_negative_profit_is_kept():
    (turn,) = from_rows([{"speaker": "A", "round": 1, "action": 5, "profit": -3.5}], "2026-10-22T00:00:00Z")
    assert turn.profit == -3.5


def test_a_pi_price_below_0_or_a_schema_refusal_is_a_log_import_error(tmp_path):
    bad = pi_session("A", [(None, -1.0)])
    with pytest.raises(LogImportError, match="set_price price -1.0 is not a finite number of 0 or more"):
        import_pi({"A": bad.encode(), "B": pi_session("B", [(None, 9.0)]).encode()})
    rows = json.dumps([{"speaker": "A", "round": 1, "action": 5, "model": 7}, {"speaker": "B", "round": 1, "action": 5}])
    with pytest.raises(LogImportError, match="model"):
        import_rows("log.json", rows.encode(), "2026-10-22T00:00:00Z")


def test_each_seller_has_the_grants_of_its_moves_and_a_private_id_is_sorted():
    """CR-76 major 1: a run needs set_price, and post on each channel a seller posted on; private_B_A is the pair A, B."""
    rows = [
        {"speaker": "B", "round": 1, "text": "hold?", "channel": "private_B_A", "action": 9},
        {"speaker": "A", "round": 1, "text": "yes", "channel": "private_A_B", "action": 9},
        {"speaker": "C", "round": 1, "action": 8},
    ]
    imported = build(from_rows(rows, "2026-10-22T00:00:00Z"), [b"x"], "2026-10-22T00:00:00Z")
    assert [c.id for c in imported.header.channels] == ["private_A_B"]
    grants = {a.id: [(g.tool_id, g.params) for g in a.capability.grants] for a in imported.header.agents}
    post = ("post", {"channels": ["private_A_B"]})
    assert grants == {
        "A": [("set_price", {}), ("hold", {}), post],
        "B": [("set_price", {}), ("hold", {}), post],
        "C": [("set_price", {}), ("hold", {})],
    }
    events = check_episode(imported)
    assert [(e.agent_id, e.channel_id, e.receivers) for e in events if e.type == "posted"] == [
        ("A", "private_A_B", []),
        ("B", "private_A_B", []),
    ]


def test_key_like_strings_in_the_trail_are_redacted():
    """CR-76 minor 2: the stored trail has no key-like string; the rest of the text stays."""
    rows = [
        {"speaker": "A", "round": 1, "action": 5, "thinking": 'my key is sk-abcdefgh12345678 and {"api_key": "zz9"}'},
        {"speaker": "B", "round": 1, "action": 5, "text": "api-key=hunter2 then skip-this"},
    ]
    text = import_rows("log.json", json.dumps(rows).encode(), "2026-10-22T00:00:00Z").trail_text()
    assert "sk-abcdefgh" not in text and "zz9" not in text and "hunter2" not in text
    assert text.count("[redacted]") == 3 and "my key is" in text and "skip-this" in text


def test_a_pi_file_that_is_not_a_session_is_refused(tmp_path):
    bad = tmp_path / "x.jsonl"
    bad.write_text('{"type": "message"}\n', encoding="utf-8")
    with pytest.raises(LogImportError, match="not a Pi session export"):
        import_pi({"A": bad.read_bytes(), "B": bad.read_bytes()})


# The script


def test_the_script_writes_the_episode_the_trail_and_the_feature_row(sessions, tmp_path, capsys):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    try:
        from import_logs import main
    finally:
        sys.path.pop(0)
    out, features = tmp_path / "out", tmp_path / "features.csv"
    assert main(["pi", f"A={tmp_path / 'a.jsonl'}", f"B={tmp_path / 'b.jsonl'}", "-o", str(out), "--features", str(features)]) == 0
    printed = json.loads(capsys.readouterr().out)
    folder = out / printed["run_id"]
    read_episode((folder / "episode.jsonl").read_text(encoding="utf-8").splitlines())
    assert len((folder / "trail.jsonl").read_text(encoding="utf-8").splitlines()) == printed["trail_lines"] == 12
    header = features.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header[:3] == ["run_id", REGISTRY.ids()[0], f"{REGISTRY.ids()[0]}_missing"]
    assert main(["rows", str(tmp_path / "a.jsonl"), "-o", str(out)]) == 2, "a Pi file is not a rows log"
