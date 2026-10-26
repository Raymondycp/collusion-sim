# ruff: noqa: F401, F811 (fixtures from test_api, test_d13 and test_experiments)
"""DECISIONS-20: one folder per run (runs/<run_id>/) and per experiment (experiments/<id>/) with the whole bundle, the
same bytes as the zips, and no change to the episode hash or the ledger (DE-run-folder-report.md §2)."""

import hashlib
import json
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from services import read
from services.read import write_run_folder
from test_api import check, client, config, start
from test_d13 import fake_client, run_condition, talker
from test_experiments import body, created, finished

BUNDLE = ["episode.jsonl", "meta.json", "trail.jsonl"]


def folder(c, rid: str) -> Path:
    return c.app.state.svc.runs_dir / rid


def manifest(path: Path) -> dict:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def zipped(c, url: str, **params) -> zipfile.ZipFile:
    resp = c.get(url, params={"format": "zip", **params})
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(BytesIO(resp.content))


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def snapshot(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(path.iterdir())}


def test_a_finished_llm_run_has_its_whole_bundle_with_the_zips_bytes(fake_client):
    """Cases 1, 6, 9: the folder holds the bundle, its manifest has each file's bytes and sha256, the run zip holds the
    same bytes, and the ledger's export hash is the folder episode's hash."""
    fake_client.fake.brain = talker
    events, _ = run_condition(fake_client, "C1")
    rid = events[0]["run_id"]
    here = folder(fake_client, rid)
    assert sorted(p.name for p in here.iterdir()) == sorted([*BUNDLE, "manifest.json"])
    m = manifest(here)
    assert (m["manifest"], m["run_id"], m["branches"]) == ("run-folder.v1", rid, [])
    assert [f["path"] for f in m["files"]] == BUNDLE
    for f in m["files"]:
        data = (here / f["path"]).read_bytes()
        assert (f["bytes"], f["sha256"]) == (len(data), sha(data)), f["path"]
    z = zipped(fake_client, f"/api/runs/{rid}/export")
    for name in BUNDLE:
        assert z.read(name) == (here / name).read_bytes(), name
    assert (here / "trail.jsonl").read_bytes().count(b"\n") == sum(e["type"] == "llm_call" for e in events) > 0
    (line,) = fake_client.app.state.svc.db.ledger(rid)
    assert line["export_hash"] == sha((here / "episode.jsonl").read_bytes())
    assert check(fake_client.get("/api/ledger/verify"), "/ledger/verify")["ok"]


def branch(c, rid: str, n: int = 2) -> list[str]:
    iv = {"type": "probe", "target": "A", "price": "nash", "rounds": 1}
    ids = check(
        c.post(f"/api/runs/{rid}/branches", json={"at": 15, "rounds": 5, "n": n, "intervention": iv}), "/runs/{run_id}/branches", "post"
    )
    for child in ids["run_ids"]:
        c.app.state.svc.wait(child, timeout=30)
    return sorted(ids["run_ids"])


def test_branch_folders_are_listed_in_the_parents_manifest_and_a_rewrite_gives_the_same_bytes(client):
    """Cases 3, 4, 6: each branch has its own folder; the parent (ended first) lists them, sorted; the zip with branches
    holds the same bytes; writing the folders again changes no byte."""
    (rid,) = start(client)
    assert manifest(folder(client, rid))["branches"] == []
    kids = branch(client, rid)
    assert manifest(folder(client, rid))["branches"] == [{"run_id": k, "path": f"../{k}/"} for k in kids]
    z = zipped(client, f"/api/runs/{rid}/export", include="branches")
    for k in kids:
        assert manifest(folder(client, k))["run_id"] == k
        for name in BUNDLE:
            assert z.read(f"branches/{k}/{name}") == (folder(client, k) / name).read_bytes()
    before = {r: snapshot(folder(client, r)) for r in [rid, *kids]}
    svc = client.app.state.svc
    for r in [*kids, rid]:
        write_run_folder(svc.db, svc.runs_dir, svc.cache_path, r)
    assert {r: snapshot(folder(client, r)) for r in [rid, *kids]} == before


def test_two_branches_that_end_together_are_both_in_the_parents_manifest(client, monkeypatch):
    """Case 5 (CR-86 minor 1): the first child to rewrite the parent's manifest is held after it listed the children;
    the second child ends meanwhile. Without the lock the first write drops the second child."""
    (rid,) = start(client)
    kids = branch(client, rid)
    svc = client.app.state.svc
    for k in kids:
        (folder(client, k) / "manifest.json").unlink()
    parent = folder(client, rid) / "manifest.json"
    put, held = read._put, []

    def slow(path, data):
        if path == parent and not held:
            held.append(path)
            time.sleep(0.5)
        put(path, data)

    monkeypatch.setattr(read, "_put", slow)
    threads = [threading.Thread(target=write_run_folder, args=(svc.db, svc.runs_dir, svc.cache_path, k)) for k in kids]
    threads[0].start()
    time.sleep(0.1)
    threads[1].start()
    for t in threads:
        t.join()
    assert held and [b["run_id"] for b in manifest(folder(client, rid))["branches"]] == kids


def test_a_rewrite_that_fails_leaves_no_manifest_and_the_parent_drops_the_child(client, monkeypatch):
    """CR-86 minor 2 and addendum: a folder with a manifest is a whole bundle, also after a failed second write, and the
    parent's manifest then lists only the children that have one."""
    (rid,) = start(client)
    kids = branch(client, rid)
    svc = client.app.state.svc
    assert (folder(client, kids[0]) / "manifest.json").is_file()
    put = read._put

    def full(path, data):
        if path.name == "trail.jsonl":
            raise OSError("disk full")
        put(path, data)

    monkeypatch.setattr(read, "_put", full)
    with pytest.raises(OSError):
        write_run_folder(svc.db, svc.runs_dir, svc.cache_path, kids[0])
    assert not (folder(client, kids[0]) / "manifest.json").exists()
    assert [b["run_id"] for b in manifest(folder(client, rid))["branches"]] == kids[1:]


def test_a_failed_run_has_its_folder(client, monkeypatch):
    """Case 1: a failure inside the run is recorded as a failed run, and the folder is written as for any ended run."""
    from simcore.scripted import GrimTrigger

    act = GrimTrigger.act

    def broken(self, obs, tools):
        if obs.t == 3:
            raise RuntimeError("boom")
        return act(self, obs, tools)

    monkeypatch.setattr(GrimTrigger, "act", broken)
    (rid,) = check(client.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
    assert client.app.state.svc.wait(rid, timeout=30)["status"] == "failed"
    assert json.loads((folder(client, rid) / "meta.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert [f["path"] for f in manifest(folder(client, rid))["files"]] == BUNDLE


def test_an_experiment_has_its_folder_with_every_run_and_the_zips_bytes(fake_client):
    """Cases 7, 8: experiments/<id>/ holds experiment.json and a manifest of the run folders; the experiment zip holds
    the same experiment.json and the same run files."""
    exp = created(fake_client, body())
    at_create = fake_client.app.state.experiments.experiments_dir / exp["id"] / "manifest.json"
    assert at_create.is_file(), "CR-86 minor 3: the folder is written at create"
    detail = finished(fake_client, exp["id"])
    runs = sorted([c["parent"]["run_id"] for c in detail["cells"]] + [b["run_id"] for c in detail["cells"] for b in c["branches"]])
    here = fake_client.app.state.experiments.experiments_dir / exp["id"]
    deadline = time.monotonic() + 30  # the experiment worker writes the folder after the last run's hook
    while time.monotonic() < deadline and not (
        (here / "manifest.json").is_file() and [r["run_id"] for r in manifest(here)["runs"]] == runs
    ):
        time.sleep(0.2)
    m = manifest(here)
    assert (m["manifest"], m["experiment_id"]) == ("experiment-folder.v1", exp["id"])
    assert m["runs"] == [{"run_id": r, "path": f"../../runs/{r}/", "status": "completed"} for r in runs]
    data = (here / "experiment.json").read_bytes()
    assert m["files"] == [{"path": "experiment.json", "bytes": len(data), "sha256": sha(data)}]
    z = zipped(fake_client, f"/api/experiments/{exp['id']}/export")
    assert z.read("experiment.json") == data
    for r in runs:
        assert (here / m["runs"][runs.index(r)]["path"] / "manifest.json").resolve().is_file()
        for name in BUNDLE:
            assert z.read(f"runs/{r}/{name}") == (folder(fake_client, r) / name).read_bytes(), (r, name)
