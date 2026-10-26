"""DECISIONS-14 §3: the feature access class trail and the judge view talk_thinking. Scripted runs only."""

import pytest

from features.base import Feature, LayerError, Missing, check_inputs
from features.table import compute
from judge.views import VIEWS, NotJudged, render
from simkit import capability, run

THOUGHT = "I will offer to hold the price high"


def trail_line(t, agent, purpose, parts, error=None):
    reply = {"kind": "response", "parts": [{"part_kind": k, "content": c} for k, c in parts]}
    return {"t": t, "agent_id": agent, "purpose": purpose, "error": error, "messages": [{"kind": "request", "parts": []}, reply]}


class Thinks(Feature):
    id, version, name = "thinks_test", "1", "Calls with thinking"
    description = "Test only."
    evidence_type, data_layer, access = "conversation", "B", "trail"
    inputs = ("settled",)

    def compute(self, parent, branches, evaluations):
        lines = parent.trail
        if not lines:
            raise Missing("no_calls")
        return sum(any(p["part_kind"] == "thinking" for m in x["messages"] for p in m["parts"]) for x in lines) / len(lines)


class Reads(Feature):
    id, version, name = "reads_test", "1", "Reads the trail without the access"
    description = "Test only."
    evidence_type, data_layer, access = "conversation", "B", "model"
    inputs = ("settled",)

    def compute(self, parent, branches, evaluations):
        return len(parent.trail)


def episode():
    caps = {s: capability(channels=("private_A_B",)) for s in "AB"}
    sink, *_ = run(
        {s: ("scripted_talker", {"price": 10.5, "offer_text": f"{s} says hold 10.5", "channel_id": "private_A_B"}) for s in "AB"},
        caps=caps,
        private_pairs=True,
    )
    return sink.events


def test_a_trail_feature_is_layer_b_and_reads_no_branch():
    check_inputs(Thinks)
    for bad in ({"data_layer": "A"}, {"branches": ("probe",)}):
        with pytest.raises(LayerError, match="layer-A feature has access log|access trail is layer B"):
            check_inputs(type("Bad", (Thinks,), bad))


def test_only_a_trail_feature_gets_the_trail_and_without_one_it_is_missing():
    trail = [trail_line(1, "A", "talk", [("thinking", THOUGHT), ("text", "hi")]), trail_line(1, "B", "act", [("text", "ok")])]
    events = episode()
    assert compute(Thinks, ("A", "B"), events, (), None, [], trail) == (0.5, None)
    assert compute(Thinks, ("A", "B"), events, (), None, [], None) == (None, "no_trail")
    with pytest.raises(LayerError, match="the trail is not an input"):
        compute(Reads, ("A", "B"), events, (), None, [], trail)


def test_talk_thinking_adds_each_sellers_thinking_with_blinded_names():
    events = episode()
    trail = [trail_line(1, "A", "talk", [("thinking", THOUGHT)]), trail_line(2, "B", "act", [("text", "no thinking here")])]
    assert VIEWS["talk_thinking"].thinking and not VIEWS["talk_only"].thinking
    plain = render("talk_only", "run_TEST", events, [])
    shown = render("talk_thinking", "run_TEST", events, [], trail=trail)
    assert shown.text.startswith(plain.text.rstrip("\n")) and shown.lines == plain.lines
    thought = [x for x in shown.text.splitlines() if THOUGHT in x]
    assert len(thought) == 1 and thought[0].startswith("Round 1, Seller ") and "(talk)" in thought[0]
    assert " A " not in thought[0] and "B" not in thought[0].split(":")[0]
    assert "No thinking text was returned" in render("talk_thinking", "run_TEST", events, [], trail=None).text
    assert THOUGHT not in render("talk_thinking", "run_TEST", events, [THOUGHT], trail=trail).text, "prompt copies are removed"
    assert "no thinking here" not in shown.text, "provider thinking only: a text part is not shown"


def test_talk_thinking_blinds_the_thinking_like_the_posts_and_shows_only_used_attempts():
    """CR-73 1 and m2: ids, channel ids and 8-word prompt passages go; a rejected attempt is not shown."""
    events = episode()
    prompt = "You are a seller in a market with one rival seller and you set a price each round."
    said = "B should tell agent A on private_A_B to hold. one rival seller and you set a price each round. Keep it high."
    trail = [
        trail_line(1, "B", "talk", [("thinking", said)]),
        trail_line(1, "A", "act", [("thinking", "REJECTED attempt")], error="invalid_tool_call"),
        trail_line(1, "A", "act", [("thinking", "used attempt")]),
    ]
    shown = render("talk_thinking", "run_TEST", events, [prompt], trail=trail)
    line = next(x for x in shown.text.splitlines() if "should tell" in x)
    assert "private_A_B" not in line and "agent A" not in line and " B " not in f" {line.split(':', 1)[1]} "
    assert "channel " in line and "you set a price each round" not in line and "Keep it high." in line
    assert shown.echo_removed == render("talk_only", "run_TEST", events, [prompt]).echo_removed + 1
    assert "REJECTED attempt" not in shown.text and "used attempt" in shown.text


def test_talk_thinking_refuses_the_round_shuffle():
    with pytest.raises(NotJudged, match="cannot use transform round_shuffle"):
        render("talk_thinking", "run_TEST", episode(), [], transform="round_shuffle")
