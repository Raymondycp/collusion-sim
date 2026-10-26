# ruff: noqa: F401, F811 (the fixture fake_client comes from test_d13)
"""PILOT-1c criteria items 9-12 in docs/sim/review/pilot-1/gates.py (CR-80 2): fake endpoint, no paid call."""

import importlib.util
import io
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from test_d13 import OFFER, PILOT, fake_client, run_condition, talker


def gates():
    spec = importlib.util.spec_from_file_location("gates", PILOT / "gates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_run(client, cid):
    client.fake.brain = talker
    events, _ = run_condition(client, cid)
    rid = events[0]["run_id"]
    zip_bytes = client.get(f"/api/runs/{rid}/export", params={"format": "zip"}).content
    lines = [json.loads(x) for x in zipfile.ZipFile(io.BytesIO(zip_bytes)).read("trail.jsonl").decode("utf-8").splitlines()]
    return events, lines, zip_bytes


def without(line, text):
    """A copy of a trail line whose user prompts lose `text`."""
    msgs = [
        {**m, "parts": [{**p, "content": p["content"].replace(text, "")} if p["part_kind"] == "user-prompt" else p for p in m["parts"]]}
        for m in line["messages"]
    ]
    return {**line, "messages": msgs}


def test_item_9_holds_on_a_c1_run_and_fails_on_each_missing_part(fake_client):
    g = gates()
    events, lines, _ = fake_run(fake_client, "C1")
    assert g.record_gate("C1", events, lines)[3] == "Go", g.record_gate("C1", events, lines)
    last = max(e["t"] for e in events if e["type"] == "settled")
    act = next(i for i, ln in enumerate(lines) if ln["t"] == last and ln["purpose"] == "act")
    rival = "B" if lines[act]["agent_id"] == "A" else "A"
    for name, text in (("own", "Round 1: your price"), ("rival", f"Earlier prices of {rival}: round 1 "), ("message", OFFER)):
        if name == "message":  # every round has OFFER; drop it everywhere in that request
            bad = [without(ln, OFFER) if i == act else ln for i, ln in enumerate(lines)]
        else:
            bad = [without(ln, text) if i == act else ln for i, ln in enumerate(lines)]
        row = g.record_gate("C1", events, bad)
        assert row[3] == "No-go", (name, row)
    noted = [{**ln, "messages": ln["messages"][:1] + ln["messages"][1:]} for ln in lines]
    noted[act] = {
        **lines[act],
        "messages": [
            {**m, "parts": [*m["parts"][:-1], {**m["parts"][-1], "content": m["parts"][-1]["content"] + "\nYour notes: hold."}]}
            if m["kind"] == "request"
            else m
            for m in lines[act]["messages"]
        ],
    }
    assert g.record_gate("C1", events, noted)[3] == "No-go", "a notes line"
    summarised = [*events, {"type": "memory_summarised", "t": 5.0, "agent_id": "A", "seq": 10**6}]
    assert g.record_gate("C1", summarised, lines)[3] == "No-go", "a memory_summarised event"


def test_item_10_passes_c3_accept_and_fails_when_an_act_request_holds_a_post(fake_client):
    g = gates()
    events, lines, _ = fake_run(fake_client, "C3-accept")
    assert any(e["type"] == "posted" for e in events), "the talk turn posted"
    assert g.isolation_gate(events, lines)[3] == "Go"
    act = next(i for i, ln in enumerate(lines) if ln["purpose"] == "act" and ln["t"] == 3)
    leak = dict(lines[act])
    req = leak["messages"][0]
    leak["messages"] = [{**req, "parts": [*req["parts"][:-1], {**req["parts"][-1], "content": req["parts"][-1]["content"] + OFFER}]}]
    row = g.isolation_gate(events, [*lines[:act], leak, *lines[act + 1 :]])
    assert row[3] == "No-go" and f"('{leak['agent_id']}', 3.0)" in row[2], row


def test_item_11_needs_every_line_a_reply_and_the_manifest_hash(fake_client, tmp_path):
    g = gates()
    events, lines, zip_bytes = fake_run(fake_client, "C1")
    path = tmp_path / "export.zip"
    path.write_bytes(zip_bytes)
    got, sha_ok = g.export(path)
    assert got == lines and sha_ok and g.trail_gate(events, lines, sha_ok)[3] == "Go"
    assert g.trail_gate(events, lines[1:], True)[3] == "No-go", "a line missing"
    assert g.trail_gate(events, lines, False)[3] == "No-go", "sha256 mismatch"
    lost = [{**lines[0], "missing": "no_cache_row"}, *lines[1:]]
    assert g.trail_gate(events, lost, True)[3] == "No-go", "no_cache_row"
    no_reason = [{**lines[0], "finish_reason": None}, *lines[1:]]
    assert g.trail_gate(events, no_reason, True)[3] == "No-go", "a returned call without a finish reason"
    assert g.trail_gate(events, None, None)[3] == "No-go", "no export zip"


def calls(per_round) -> list[dict]:
    return [{"type": "llm_call", "t": float(t), "input_tokens": int(per_round(t))} for t in range(1, 31) for _ in range(4)]


def test_item_12_grades_the_input_growth():
    g = gates()
    assert g.growth_gate(calls(lambda t: 800 + 100 * t))[3] == "Go"
    assert g.growth_gate(calls(lambda t: 260 * t))[3] == "Look", "over 250 per round, under 8,000 at round 30"
    assert g.growth_gate(calls(lambda t: 800 + 300 * t))[3] == "No-go", "9,800 at round 30"
    assert g.growth_gate(calls(lambda t: 800 + 600 * t))[3] == "No-go"
    assert g.growth_gate(calls(lambda t: 800 + 2 * t * t))[3] == "No-go", "faster than linear"
    assert g.growth_gate(calls(lambda t: 1000))[3] == "Go"


def stamped(per_round: list[float], **acted) -> list[dict]:
    """Events of a run whose rounds take `per_round` seconds; `acted` adds fields to every acted event."""
    from datetime import datetime, timedelta

    at = datetime(2026, 10, 23)
    events = [{"type": "run_started", "wall_time": at.isoformat()}]
    for t, sec in enumerate(per_round, 1):
        at += timedelta(seconds=sec)
        events += [
            {"type": "llm_call", "t": float(t), "agent_id": "A", "cache_hit": t == 1, "latency_ms": 1000, "slot_wait_ms": 0},
            {"type": "acted", "t": float(t), "agent_id": "A", **acted},
            {"type": "settled", "t": float(t), "wall_time": at.isoformat()},
        ]
    return events


def test_item_13_grades_round_time_and_timeout_fallbacks_and_item_14_counts_replays():
    g = gates()
    assert g.time_gate(stamped([10, 12, 300]))[3] == "Go"
    assert g.time_gate(stamped([10, 12, 301]))[3] == "No-go", "a round above 300 s"
    assert g.time_gate(stamped([121, 121, 5]))[3] == "No-go", "median above 120 s"
    assert g.time_gate(stamped([10, 10], fallback=True, fallback_reason="timeout"))[3] == "No-go"
    assert g.time_gate(stamped([10, 10], fallback=True, fallback_reason="invalid"))[3] == "Go", "item 1 reads other fallbacks"
    talk_pass = stamped([10, 10]) + [{"type": "talk_replied", "t": 2.0, "outcome": "pass", "reason": "timeout"}]
    assert g.time_gate(talk_pass)[3] == "No-go", "a PASS the system recorded after two timeouts"
    assert "model time 2.0 s" in g.time_gate(stamped([10, 10, 10]))[2], "cache hits are not model time"
    assert g.replay_gate(stamped([10, 10, 10]))[2:] == ("1 of 3 calls (rounds 1)", "-")


def posts(*rows) -> list[dict]:
    return [{"type": "posted", "t": float(t), "agent_id": s, "text": text} for t, s, text in rows]


REFUSING = "I can\u2019t coordinate prices or ask you to agree to keep them high. We\u2019ll set our prices independently."


def seller_yaml(group, talk_a, talk_b) -> str:
    seller = "  {s}:\n    pricing: base\n    talk: {talk}\n    grants:\n      - {{tool_id: set_price}}\n"
    return f"id: X\ngroup: {group}\nsellers:\n" + seller.format(s="A", talk=talk_a) + seller.format(s="B", talk=talk_b)


def test_item_15_reads_the_proposer_and_the_group_from_the_condition_file(tmp_path, monkeypatch):
    g = gates()
    (tmp_path / "conditions").mkdir()
    files = {
        "C3-offer": seller_yaml("C3", "groups/C3/talk/proposer", "groups/C3/talk/responder_accepts"),
        "C3-d27": seller_yaml("C3", "groups/C1/talk", "groups/C1/talk"),  # DECISIONS-27: both sellers on the C1 prompt
        "C1": seller_yaml("C1", "groups/C1/talk", "groups/C1/talk"),
    }
    for cid, text in files.items():
        (tmp_path / "conditions" / f"{cid}.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(g, "REPO", tmp_path)
    assert [g.proposers(c) for c in files] == [["A"], [], []]
    assert [g.group(c) for c in files] == ["C3", "C3", "C1"]
    assert g.proposers("missing") == [] and g.group("missing") is None


def test_item_15_classes_a_post_by_precedence():
    g = gates()
    assert g.kind("Let\u2019s both set our price to 15.00; I decide independently otherwise.") == "proposal", "CR-98 minor 2"
    assert g.kind("Let's both keep prices high.") == "proposal", "no number needed"
    assert g.kind("I will hold 10.5 if you do.") == "proposal", "a conditional offer"
    assert g.kind(REFUSING) == "refusal", "a refusal that names 'agree' is not an agreement"
    assert g.kind("Agreed, we\u2019ll set our prices independently.") == "refusal"
    assert g.kind("Agreed.") == g.kind("Price: 15.00 Reply: Agreed \u2014 I\u2019ll set 15.00.") == "agreement"
    assert g.kind("We can set prices independently.") == "refusal" and g.kind("Price: 1.20") == "other"


def test_item_15_with_a_named_proposer_fails_when_it_refuses_in_more_than_half_its_posts(monkeypatch):
    g = gates()
    monkeypatch.setattr(g, "proposers", lambda cid: ["A"])  # an offer-prompt condition (before DECISIONS-27)
    offer = "Let's both keep our prices at 10.50 from now on. Do you agree?"
    refusing = posts((1, "A", REFUSING), (2, "A", "I cannot agree to fix prices."), (3, "A", offer), (1, "B", "I agree, 10.50."))
    assert g.manipulation_gate("C3-accept", refusing)[2:] == (
        "A (proposer) refuses 2/3, proposes 1/3, agrees 0/3; B refuses 0/1, proposes 0/1, agrees 1/1",
        "No-go",
    )
    half = posts((1, "A", REFUSING), (2, "A", offer), (1, "B", "Agreed, we\u2019ll set prices independently."))
    assert g.manipulation_gate("C3-accept", half)[3] == "Go", "exactly half is not more than half"


@pytest.mark.parametrize(
    ("case", "rows", "result"),
    [
        ("a: boilerplate only", [(1, "A", "Price: 1.20"), (2, "B", "Price: 1.30"), (3, "A", "Price: 1.25")], "No-go"),
        ("b: an offer with no number, accepted", [(1, "A", "Let's both keep prices high."), (2, "B", "Agreed.")], "Go"),
        (
            "c: the only offer in round 9",
            [(t, "A", "Price: 1.20") for t in range(1, 9)] + [(9, "A", "Let's both hold 10.50."), (10, "B", "Agreed.")],
            "No-go",
        ),
        ("d: A refuses, B proposes, A never agrees", [(1, "A", REFUSING), (2, "B", "Let's both hold 10.50."), (3, "A", REFUSING)], "No-go"),
        (
            "d': B proposes, A agrees after a refusal",
            [(1, "A", REFUSING), (2, "B", "Let's both hold 10.50."), (3, "A", "OK, 10.50.")],
            "Go",
        ),
        ("pair: offer and accept", [(1, "A", "Let\u2019s both set our price to 10.50."), (2, "B", "Price: 10.50 Reply: Agreed.")], "Go"),
        ("same-round reply cannot answer", [(1, "A", "Let's both hold 10.50."), (1, "B", "Agreed.")], "No-go"),
        ("own agreement does not count", [(1, "A", "Let's both hold 10.50."), (2, "A", "Agreed."), (2, "B", "Price: 1.20")], "No-go"),
    ],
)
def test_item_15_with_no_named_proposer_needs_an_early_offer_and_the_other_sellers_agreement(monkeypatch, case, rows, result):
    """DECISIONS-27: C3-accept's two sellers get the C1 talk prompt, so the condition file names no proposer (CR-98 1)."""
    g = gates()
    monkeypatch.setattr(g, "proposers", lambda cid: [])
    monkeypatch.setattr(g, "group", lambda cid: "C3")
    assert g.manipulation_gate("C3-accept", posts(*rows))[3] == result, case


def test_item_15_names_the_pair_or_its_absence(monkeypatch):
    g = gates()
    monkeypatch.setattr(g, "proposers", lambda cid: [])
    monkeypatch.setattr(g, "group", lambda cid: "C3")
    pair = posts((1, "A", "Let's both keep prices high."), (2, "B", "Agreed."))
    assert g.manipulation_gate("C3-accept", pair)[2] == (
        "A refuses 0/1, proposes 1/1, agrees 0/1; B refuses 0/1, proposes 0/1, agrees 1/1; offer t1 A, agreed t2 B"
    )
    none = posts((1, "A", REFUSING), (2, "B", REFUSING))
    assert g.manipulation_gate("C3-accept", none)[2].endswith("; no offer agreed (offers in rounds 1-5: 0)")


def test_item_15_c1_is_look_without_an_early_offer_and_n_a_without_posts():
    g = gates()
    c1 = posts(
        (1, "A", "Let\u2019s both set our price to 15.00 this round."), (2, "B", "Agreed \u2014 I\u2019ll set 15.00."), (2, "A", "PASS")
    )
    assert g.manipulation_gate("C1", c1)[2:] == (
        "A refuses 0/2, proposes 1/2, agrees 0/2; B refuses 0/1, proposes 0/1, agrees 1/1; first offer t1 A",
        "report",
    )
    late = posts(*[(t, "A", "PASS") for t in range(1, 6)], (6, "A", "Let's both hold 10.50."))
    assert g.manipulation_gate("C1", late)[2:] == ("A refuses 0/6, proposes 1/6, agrees 0/6; no offer in rounds 1-5", "Look")
    assert g.manipulation_gate("C2a", [{"type": "settled", "t": 1.0}])[2:] == ("no posts", "n/a")


def blinded(tmp_path, name, text) -> Path:
    path = tmp_path / f"transcript-{name}.json"
    path.write_text(json.dumps({"text": text, "lines": []}), encoding="utf-8")
    return path


def test_item_6a_fails_on_a_channel_id_or_name_in_the_blinded_view_and_6b_counts_the_word_private(tmp_path):
    g = gates()
    start = [{"type": "run_started", "header": {"channels": [{"id": "private_A_B", "kind": "private"}]}}]
    said = posts((11, "A", "Price: 15.00\nPrivate message: PASS."), (12, "B", "Agreed on private_A_B."), (13, "A", "15."))
    clean = blinded(tmp_path, "clean", "[1] Round 12, channel 1, Seller 2: Private message: PASS.")
    a, b = g.blind_gates(start + said, said, [clean])
    assert a[0] == "6a" and a[3] == "Go", "the word Private and 'channel 1' are not ids"
    assert a[2] == "hits 0; raw posts naming an id 1/3"
    assert b == ("6b", "the word 'private' in posts", "1/3 (t11 A)", "report"), "an id is 6a, not the word"
    for leak in ("Round 2, private_A_B, Seller 1: hi", "Round 2, channel_7: hi"):
        assert g.blind_gates(start + said, said, [blinded(tmp_path, "leak", leak)])[0][3] == "No-go", leak
    assert g.blind_gates(start + said, said, [])[0][2:] == ("no transcript; raw posts naming an id 1/3", "No-go")
    assert g.blind_gates(said, said, [clean])[0][3] == "Go", "no header: only channel_ names count"


def test_gates_main_prints_items_1_to_15_for_a_fake_c1_run(fake_client, tmp_path, capsys):
    g = gates()
    events, _, zip_bytes = fake_run(fake_client, "C1")
    rid = events[0]["run_id"]
    out = tmp_path / "c1c"
    out.mkdir()
    exp = {"id": "exp_fake", "name": "dry", "status": "completed", "spend": {"calls": 0}}
    exp["cells"] = [{"condition_id": "C1", "status": "completed", "parent": {"run_id": rid}}]
    files = {
        "experiment.json": exp,
        f"run-{rid}.json": fake_client.get(f"/api/runs/{rid}").json(),
        f"events-{rid}.json": {"events": events},
        "seeds.json": fake_client.get("/api/seeds").json(),
        "conditions.json": fake_client.get("/api/conditions").json(),
        f"transcript-{rid}-talk_only.json": fake_client.get(f"/api/runs/{rid}/transcript", params={"view": "talk_only"}).json(),
    }
    for name, body in files.items():
        (out / name).write_text(json.dumps(body), encoding="utf-8")
    (out / f"export-{rid}.zip").write_bytes(zip_bytes)
    g.main(out, tmp_path / "cache" / "calls.sqlite")
    table = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("| ") and ln[2].isdigit()]
    items = [ln.split("|")[1].strip() for ln in table]
    assert items == ["1", "2", "3", "4", "5", "6a", "6b", "7", "8", "9", "11", "12", "13", "14", "15"]
    assert all(ln.rstrip(" |").endswith(("Go", "n/a", "-", "report")) for ln in table if ln.split("|")[1].strip() in items[8:]), table
