# ruff: noqa: F811 (the fixture client comes from test_experiments)
"""Archive (DECISIONS-29, contract 0.39.0): archived runs and experiments leave the lists until include_archived=true, ids or
all, a live run is refused, and nothing is deleted: reads by id, exports and features stay the same."""

import pytest

from test_api import check, config
from test_experiments import body, client, created, finished  # noqa: F401


def start(c, seeds=(1, 2, 3)) -> list[str]:
    run_ids = check(c.post("/api/runs", json=config(seeds=seeds)), "/runs", "post")["run_ids"]
    for rid in run_ids:
        c.app.state.svc.wait(rid, timeout=30)
    return run_ids


def listed(c, path: str, **params) -> dict[str, dict]:
    got = check(c.get(f"/api/{path}", params=params), f"/{path}")
    return {r["id"]: r for r in (got["items"] if path == "runs" else got)}


def archive(c, path: str, action: str, payload: dict, status: int = 200) -> dict:
    resp = c.post(f"/api/{path}/{action}", json=payload)
    assert resp.status_code == status, resp.text
    return check(resp, f"/{path}/{action}", "post")


def set_status(c, rid: str, status: str = "running") -> None:
    """A run in another state, as a long run, a crash or a queue leaves it."""
    with c.app.state.db.write_conn() as conn:
        conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, rid))


def test_archived_runs_leave_the_list_and_come_back_with_include_archived(client):
    r0, r1, r2 = start(client)
    assert archive(client, "runs", "archive", {"ids": [r0]}) == {"count": 1, "skipped": []}
    assert set(listed(client, "runs")) == {r1, r2}
    every = listed(client, "runs", include_archived="true")
    assert set(every) == {r0, r1, r2} and every[r0]["archived_at"] and every[r1]["archived_at"] is None
    assert check(client.get(f"/api/runs/{r0}"), "/runs/{run_id}")["archived_at"] == every[r0]["archived_at"], "a read by id"
    assert archive(client, "runs", "archive", {"ids": [r0]})["count"] == 0, "already archived"
    assert archive(client, "runs", "unarchive", {"ids": [r0]}) == {"count": 1, "skipped": []}
    assert set(listed(client, "runs")) == {r0, r1, r2} and listed(client, "runs")[r0]["archived_at"] is None


def test_all_archives_every_run_and_skips_a_running_one(client):
    r0, r1, r2 = start(client)
    set_status(client, r2)
    assert archive(client, "runs", "archive", {"all": True}) == {"count": 2, "skipped": [r2]}
    assert set(listed(client, "runs")) == {r2}
    assert archive(client, "runs", "unarchive", {"all": True}) == {"count": 2, "skipped": []}
    assert set(listed(client, "runs")) == {r0, r1, r2}


def test_ids_with_a_running_run_is_409_and_changes_nothing(client):
    r0, r1 = start(client, seeds=(1, 2))
    set_status(client, r1)
    refused = archive(client, "runs", "archive", {"ids": [r0, r1]}, 409)
    assert refused["code"] == "conflict" and r1 in refused["message"]
    assert set(listed(client, "runs")) == {r0, r1}
    assert archive(client, "runs", "archive", {"ids": [r0, "run_unknown"]}, 404)["code"] == "not_found"
    assert set(listed(client, "runs")) == {r0, r1}


@pytest.mark.parametrize("status", ["queued", "paused", "recovering"])
def test_only_running_blocks_a_queued_paused_or_recovering_run_is_archived(client, status):
    """DECISIONS-29 entry 2 (CR-108 finding 1): a run that never started stays archivable."""
    r0, r1 = start(client, seeds=(1, 2))
    set_status(client, r0, status)
    assert archive(client, "runs", "archive", {"ids": [r0]}) == {"count": 1, "skipped": []}
    assert archive(client, "runs", "archive", {"all": True}) == {"count": 1, "skipped": []}
    assert listed(client, "runs") == {}


def test_all_ignores_the_list_filters(client):
    """DECISIONS-29 entry 2: all = every record on the server that is not archived."""
    r0, r1 = start(client, seeds=(1, 2))
    set_status(client, r1, "stopped")
    assert set(listed(client, "runs", status="completed")) == {r0}, "the list the user sees"
    assert archive(client, "runs", "archive", {"all": True}) == {"count": 2, "skipped": []}


@pytest.mark.parametrize("payload", [{}, {"all": True, "ids": ["x"]}, {"all": False}, {"ids": []}])
def test_the_body_needs_ids_or_all_true(client, payload):
    assert client.post("/api/runs/archive", json=payload).status_code == 422
    assert client.post("/api/experiments/unarchive", json=payload).status_code == 422


def test_nothing_is_deleted_and_the_run_export_does_not_change(client):
    (rid,) = start(client, seeds=(1,))
    before = client.get(f"/api/runs/{rid}/export", params={"format": "zip"}).content
    events = client.get(f"/api/runs/{rid}/events", params={"limit": 1000}).json()["events"]
    archive(client, "runs", "archive", {"ids": [rid]})
    assert client.get(f"/api/runs/{rid}/export", params={"format": "zip"}).content == before
    assert client.get(f"/api/runs/{rid}/events", params={"limit": 1000}).json()["events"] == events


def test_archived_experiments_leave_the_list_and_keep_their_reads_exports_and_features(client):
    e0 = created(client, body(seeds=(1,)))["id"]
    e1 = created(client, body(seeds=(2,)))["id"]
    finished(client, e0), finished(client, e1)
    zipped = client.get(f"/api/experiments/{e0}/export", params={"format": "zip"}).content
    features = client.get(f"/api/experiments/{e0}/features").json()
    assert archive(client, "experiments", "archive", {"ids": [e0]}) == {"count": 1, "skipped": []}
    assert set(listed(client, "experiments")) == {e1}
    every = listed(client, "experiments", include_archived="true")
    assert set(every) == {e0, e1} and every[e0]["archived_at"] and every[e1]["archived_at"] is None
    assert check(client.get(f"/api/experiments/{e0}"), "/experiments/{experiment_id}")["archived_at"] == every[e0]["archived_at"]
    assert client.get(f"/api/experiments/{e0}/export", params={"format": "zip"}).content == zipped
    assert client.get(f"/api/experiments/{e0}/features").json() == features
    assert check(client.get("/api/runs"), "/runs")["items"], "the experiment's runs stay listed"
    assert archive(client, "experiments", "archive", {"all": True})["count"] == 1
    assert listed(client, "experiments") == {}
    assert archive(client, "experiments", "unarchive", {"all": True})["count"] == 2
    assert archive(client, "experiments", "archive", {"ids": ["exp_unknown"]}, 404)["code"] == "not_found"


def test_a_running_experiment_is_409_by_ids_and_skipped_by_all(client):
    """DECISIONS-29 entry 2: experiments use the run rule; a cell with a running run makes the experiment running."""
    e0 = created(client, body(seeds=(1,)))["id"]
    e1 = created(client, body(seeds=(2,)))["id"]
    done = finished(client, e0)
    finished(client, e1)
    set_status(client, done["cells"][0]["parent"]["run_id"])
    assert check(client.get(f"/api/experiments/{e0}"), "/experiments/{experiment_id}")["status"] == "running"
    refused = archive(client, "experiments", "archive", {"ids": [e0, e1]}, 409)
    assert refused["code"] == "conflict" and e0 in refused["message"] and set(listed(client, "experiments")) == {e0, e1}
    assert archive(client, "experiments", "archive", {"all": True}) == {"count": 1, "skipped": [e0]}
    assert set(listed(client, "experiments")) == {e0}
