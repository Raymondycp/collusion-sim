"""The --follow polling loop on a fake live run (no server, no real sleep)."""

import json

import httpx2


def ev(seq, type_="round_started", **kw):
    return {"seq": seq, "run_id": "run_x", "t": 1.0, "wall_time": "", "tier": "B", "type": type_, **kw}


def fake_run(pages: list[dict], statuses: list[str]):
    """Each events request takes the next page; each run request the next status. Records the from_seq of each events request."""
    seen = []

    def handler(request):
        if request.url.path.endswith("/events"):
            seen.append(int(request.url.params["from_seq"]))
            return httpx2.Response(200, json=pages.pop(0))
        return httpx2.Response(200, json={"id": "run_x", "status": statuses.pop(0)})

    return httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(handler)), seen


def test_follow_sleeps_while_live_and_stops_at_run_ended(run_cli):
    pages = [
        {"events": [ev(0, "run_started"), ev(1)], "last_seq": 1, "next_from_seq": None},
        {"events": [], "last_seq": 1, "next_from_seq": None},
        {"events": [ev(2), ev(3, "run_ended")], "last_seq": 3, "next_from_seq": None},
    ]
    http, seen = fake_run(pages, ["running", "running"])
    sleeps = []
    r = run_cli(http, "runs", "events", "run_x", "--follow", "--json", sleep=sleeps.append)
    assert r.exit_code == 0 and [json.loads(line)["seq"] for line in r.stdout.splitlines()] == [0, 1, 2, 3]
    assert seen == [0, 2, 2] and sleeps == [1.0, 1.0]


def test_follow_with_a_filter_stops_after_the_run_is_terminal_and_one_more_page(run_cli):
    pages = [
        {"events": [ev(4, "posted", agent_id="A")], "last_seq": 9, "next_from_seq": None},
        {"events": [ev(12, "posted", agent_id="A")], "last_seq": 14, "next_from_seq": None},
    ]
    http, seen = fake_run(pages, ["completed"])
    r = run_cli(http, "runs", "events", "run_x", "--follow", "--types", "posted", "--json", sleep=lambda s: None)
    assert r.exit_code == 0 and [json.loads(line)["seq"] for line in r.stdout.splitlines()] == [4, 12]
    assert seen == [0, 10] and pages == []


def test_follow_on_a_paused_run_prints_what_it_has_then_stops_with_run_not_live(run_cli):
    pages = [{"events": [ev(0, "run_started"), ev(1)], "last_seq": 1, "next_from_seq": None}]
    http, seen = fake_run(pages, ["paused"])
    r = run_cli(http, "runs", "events", "run_x", "--follow", "--json", sleep=lambda s: None)
    lines = [json.loads(line) for line in r.stdout.splitlines()]
    assert r.exit_code == 1 and [e["seq"] for e in lines[:2]] == [0, 1]
    assert lines[-1]["code"] == "run_not_live" and "seq" not in lines[-1] and "paused" in lines[-1]["message"]


def test_follow_gives_up_after_max_wait(run_cli):
    pages = [{"events": [], "last_seq": -1, "next_from_seq": None} for _ in range(10)]
    http, seen = fake_run(pages, ["running"] * 10)
    now = [0.0]

    def tick(seconds):
        now[0] += seconds

    r = run_cli(http, "runs", "events", "run_x", "--follow", "--max-wait", "2.5", "--json", sleep=tick, clock=lambda: now[0])
    assert r.exit_code == 1 and json.loads(r.stdout.splitlines()[-1])["code"] == "wait_timeout"
    assert len(seen) == 4
