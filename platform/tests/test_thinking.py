# ruff: noqa: F401, F811 (the fixture fake_client comes from test_d13)
"""DECISIONS-14 §2.1 and §2.5: thinking or reasoning parts that the provider returns are kept in Reply, the call cache
and the trail; nothing in the request asks for them. Fake endpoint, no paid call."""

import json
import sqlite3

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, TextPart, ThinkingPart

from adapters.llm.endpoint import Reply, Request
from fakellm import cooperator
from test_d13 import fake_client, run_condition, talker
from test_llm_policy import PROFILE, llm_run
from test_trail import export, trail

THOUGHT = "Rival held 10.5 last round; holding keeps the joint price."


def thinker(body):
    """talker's reply, plus the reasoning field that DeepSeek-style chat APIs return."""
    return {**talker(body), "reasoning_content": THOUGHT}


def test_returned_thinking_is_in_the_cache_and_the_trail(fake_client, tmp_path):
    fake_client.fake.brain = thinker
    events, _ = run_condition(fake_client, "C1")
    lines = trail(export(fake_client, events[0]["run_id"]))
    assert lines and {(ln["thinking"], ln["thinking_parts"]) for ln in lines} == {(THOUGHT, 1)}
    for ln in lines:
        response = ModelMessagesTypeAdapter.validate_python(ln["messages"])[-1]
        assert isinstance(response.parts[0], ThinkingPart) and response.parts[0].content == THOUGHT
        if ln["purpose"] == "talk":
            assert [p.content for p in response.parts if isinstance(p, TextPart)] != [THOUGHT], "the answer stays apart"
    with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:
        replies = [Reply.from_json(r) for (r,) in conn.execute("SELECT reply_json FROM calls")]
    assert replies and {(r.thinking, r.thinking_parts) for r in replies} == {(THOUGHT, 1)}


def test_no_thinking_is_null_text_and_zero_parts_and_an_old_row_is_null(fake_client, tmp_path):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    lines = trail(export(fake_client, rid))
    assert {(ln["thinking"], ln["thinking_parts"]) for ln in lines} == {(None, 0)}
    assert not any(isinstance(p, ThinkingPart) for ln in lines for p in ModelMessagesTypeAdapter.validate_python(ln["messages"])[-1].parts)
    (tmp_path / "runs" / rid / "trail.jsonl").unlink()
    with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:
        for key, reply in conn.execute("SELECT key, reply_json FROM calls").fetchall():
            old = {k: v for k, v in json.loads(reply).items() if k not in ("thinking", "thinking_parts")}
            conn.execute("UPDATE calls SET reply_json = ? WHERE key = ?", (json.dumps(old, sort_keys=True), key))
    assert {(ln["thinking"], ln["thinking_parts"]) for ln in trail(export(fake_client, rid))} == {(None, None)}


def test_empty_thinking_parts_are_counted_with_null_text(fake_client):
    fake_client.fake.brain = lambda b: {**talker(b), "reasoning_content": ""}
    events, _ = run_condition(fake_client, "C1")
    assert {ln["thinking"] for ln in trail(export(fake_client, events[0]["run_id"]))} == {None}, "never an empty string"


def test_the_request_asks_for_no_thinking(fake_client):
    fake_client.fake.brain = thinker
    run_condition(fake_client, "C1")
    asks = {"reasoning", "reasoning_effort", "thinking", "include_reasoning", "extra_body"}
    bodies = fake_client.fake.bodies
    assert bodies and not any(asks & set(b) for b in bodies), "no field that asks a model for its reasoning"
    sent = [m.get("content") or "" for b in bodies for m in b["messages"]]
    assert not any(THOUGHT in text for text in sent), "returned thinking is never sent back to a model"


@pytest.mark.parametrize(
    ("supports_seed", "top_p", "max_out", "thinking", "keys"),
    [
        (True, 0.9, 400, "low", {"temperature", "max_tokens", "top_p", "seed", "thinking"}),
        (True, 0.9, 400, None, {"temperature", "max_tokens", "top_p", "seed"}),
        (False, None, None, False, {"temperature", "thinking"}),
        (False, None, None, None, {"temperature"}),
    ],
)
def test_the_request_settings_are_exactly_the_sampling_keys(tmp_path, supports_seed, top_p, max_out, thinking, keys):
    """CR-74 3: a new key in _with_settings must fail here. DECISIONS-18 entries 2 and 3: a null max_output_tokens sends
    no limit, and the one thinking key is Pydantic AI's unified effort setting, sent only when the model sets it."""
    profile = PROFILE.model_copy(update={"supports_seed": supports_seed})
    kw = {"top_p": top_p, "max_output_tokens": max_out, "thinking": thinking}
    *_, policy = llm_run(cooperator(), tmp_path, rounds=1, profile=profile, policy_kw=kw)
    req = Request(messages=[], tools=[], allow_text=False)
    settings = policy._with_settings(req, 1, 1).settings
    assert set(settings) == keys
    assert settings.get("thinking") is thinking or "thinking" not in settings
