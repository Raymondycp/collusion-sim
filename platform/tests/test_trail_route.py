# ruff: noqa: F401, F811 (the fixture fake_client comes from test_d13)
"""GET /runs/{run_id}/calls/{seq}/trail and the example docs/sim/api/examples/trail-demo.jsonl (fake endpoint, no paid call).

Write the example again with `SIM_WRITE_TRAIL_DEMO=1 uv run pytest tests/test_trail_route.py -k demo`."""

import json
import os
import sqlite3

from jsonschema import Draft202012Validator
from pydantic_ai.messages import ModelMessagesTypeAdapter

from test_api import KEYLIKE, ROOT, SPEC, check
from test_d13 import fake_client, run_condition, talker
from test_trail import export, fail_first, trail

DEMO = ROOT / "docs" / "sim" / "api" / "examples" / "trail-demo.jsonl"
ROUTE = "/runs/{run_id}/calls/{seq}/trail"


def thinker(body):
    return {**talker(body), "reasoning_content": "The rival held 10.5; holding keeps the joint price."}


def test_the_route_serves_each_line_of_the_trail_and_404_otherwise(fake_client):
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    lines = trail(export(fake_client, rid))
    for ln in lines[:6]:
        assert check(fake_client.get(f"/api/runs/{rid}/calls/{ln['seq']}/trail"), ROUTE) == ln
    started = next(e for e in events if e["type"] == "run_started")
    for path in (f"/api/runs/{rid}/calls/{started['seq']}/trail", "/api/runs/run_NOPE/calls/1/trail"):
        assert check(fake_client.get(path), ROUTE)["code"] == "not_found", path


def test_the_route_builds_the_line_for_a_run_without_a_stored_trail(fake_client, tmp_path):
    fake_client.fake.brain = fail_first(talker)
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    first = next(e for e in events if e["type"] == "llm_call")
    (tmp_path / "runs" / rid / "trail.jsonl").unlink()
    with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:
        conn.execute("DELETE FROM failed_calls")
    got = check(fake_client.get(f"/api/runs/{rid}/calls/{first['seq']}/trail"), ROUTE)
    assert (got["seq"], got["error"], got["missing"], got["messages"]) == (first["seq"], "provider_error", "no_cache_row", None)


def demo_lines(client, tmp_path) -> list[dict]:
    """Round 1 of a fake C1 run whose first call fails and whose replies carry reasoning; the failed request is then
    removed from the cache, so the first line is no_cache_row."""
    client.fake.brain = fail_first(thinker)
    events, _ = run_condition(client, "C1")
    rid = events[0]["run_id"]
    (tmp_path / "runs" / rid / "trail.jsonl").unlink()
    with sqlite3.connect(tmp_path / "cache" / "calls.sqlite") as conn:
        conn.execute("DELETE FROM failed_calls")
    return [ln for ln in trail(export(client, rid)) if ln["t"] == 1]


def test_the_trail_demo_example_is_valid_with_one_no_cache_row_line(fake_client, tmp_path):
    if os.environ.get("SIM_WRITE_TRAIL_DEMO") == "1":
        text = "".join(json.dumps(ln, sort_keys=True, ensure_ascii=False) + "\n" for ln in demo_lines(fake_client, tmp_path))
        DEMO.write_text(text, encoding="utf-8", newline="\n")
    assert not KEYLIKE.search(DEMO.read_text(encoding="utf-8")), "no key-like text in a public example"
    lines = [json.loads(x) for x in DEMO.read_text(encoding="utf-8").splitlines()]
    validator = Draft202012Validator({"$ref": "#/components/schemas/TrailLine", "components": SPEC["components"]})
    for ln in lines:
        validator.validate(ln)
        if ln["messages"] is not None:
            ModelMessagesTypeAdapter.validate_python(ln["messages"])
    assert [ln["missing"] for ln in lines].count("no_cache_row") == 1 and lines[0]["missing"] == "no_cache_row"
    assert {ln["purpose"] for ln in lines} == {"talk", "act"} and any(ln["thinking"] for ln in lines)
    assert [ln["seq"] for ln in lines] == sorted(ln["seq"] for ln in lines)
