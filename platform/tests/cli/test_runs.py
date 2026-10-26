"""Run read side against finished scripted runs (CLI-DESIGN §3.5)."""

import json


def test_list_filters_and_follows_cursors(server):
    everything = server("runs", "list", "--json").json
    assert {r["id"] for r in everything["items"]} == set(server.run_ids + server.children)
    paged = server("runs", "list", "--limit", "1", "--all", "--json").json
    assert [r["id"] for r in paged["items"]] == [r["id"] for r in everything["items"]] and paged["next_cursor"] is None
    one = server("runs", "list", "--limit", "1", "--json").json
    assert len(one["items"]) == 1 and one["next_cursor"]
    kids = server("runs", "list", "--parent", server.run_ids[0], "--json").json
    assert sorted(r["id"] for r in kids["items"]) == sorted(server.children)


def test_show_prints_the_run_detail(server):
    rid = server.run_ids[0]
    detail = server("runs", "show", rid, "--json").json
    assert detail == server.http.get(f"runs/{rid}").json() and detail["status"] == "completed"
    assert "id" in server("runs", "show", rid).stdout


def test_events_page_and_filters(server):
    rid = server.run_ids[0]
    page = server("runs", "events", rid, "--limit", "50", "--json").json
    assert [e["seq"] for e in page["events"]] == list(range(50)) and page["next_from_seq"] == 50
    posts = server("runs", "events", rid, "--types", "posted", "--agent", "A", "--json").json
    assert len(posts["events"]) == 30 and {e["type"] for e in posts["events"]} == {"posted"}
    human = server("runs", "events", rid, "--limit", "12").stdout.splitlines()
    assert "run_started" in human[0] and "price=10.5" in human[7] and human[-1] == "next from_seq: 12"


def test_follow_on_a_finished_run_prints_json_lines_up_to_run_ended(server):
    rid = server.run_ids[0]
    r = server("runs", "events", rid, "--follow", "--limit", "100", "--json", sleep=lambda s: None)
    events = [json.loads(line) for line in r.stdout.splitlines()]
    assert r.exit_code == 0 and [e["seq"] for e in events] == list(range(len(events)))
    assert events[-1]["type"] == "run_ended" and events == server.http.get(f"runs/{rid}/events", params={"limit": 5000}).json()["events"]


def test_state_at_t_and_past_the_end(server):
    rid = server.run_ids[0]
    st = server("runs", "state", rid, "--t", "12", "--json").json
    assert st["t"] == 12 and st["sellers"]["A"]["price"] == 10.5
    late = server("runs", "state", rid, "--t", "99", "--json")
    assert late.exit_code == 1 and late.json["code"] == "snapshot_missing"
    assert server("runs", "state", rid).exit_code == 2


def test_series_json_and_table(server):
    rid = server.run_ids[0]
    body = server("runs", "series", rid, "--json").json
    assert body["t"] == list(range(1, 31))
    lines = server("runs", "series", rid).stdout.splitlines()
    assert lines[0].split() == ["t", "price.A", "price.B", "nash", "joint_optimum"] and len(lines) == 31


def test_observations(server):
    rid = server.run_ids[0]
    obs = server("runs", "observations", rid, "--agent", "A", "--t", "3", "--json").json
    assert obs["agent_id"] == "A" and obs["t"] == 3 and obs["calls"] == []
