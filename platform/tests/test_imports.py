"""Unit 5b: POST /imports stores an outside log as a completed exploratory run, and the judge block `imported` judges it
outside the plan's cap. Scripted judge only; no model call."""

import io
import json
import threading
import time
import zipfile
from pathlib import Path

import jsonschema
import pytest
import yaml

from judge import batch
from judge.blocks import BLOCKS, CAP_PER_BACKEND
from judge.store import LedgerState, PlannedCall
from services import imports
from services.configs import ApiError
from services.imports import ImportCreate
from simcore.run import RunSpec
from test_api import check
from test_evaluations import body, client, evals, pilot_runs, root  # noqa: F401 (fixtures)
from test_log_import import pi_session

ROWS = (
    "speaker,round,text,channel,action\nA,1,Shall we both hold 10.5?,private_A_B,10.5\nB,1,Agreed.,private_A_B,10.5\nA,2,,,10.5\nB,2,,,9\n"
)


def pi_files():
    return {
        "A": pi_session("A", [("Hold 10.5?", 10.5), (None, 10.5), ("Keep it.", 10.0)]),
        "B": pi_session("B", [("Yes.", 10.5), (None, 9.0), (None, 9.0)], model="other-model"),
    }


def imported(client, **kw) -> dict:  # noqa: F811
    payload = {"format": "pi", "files": pi_files(), "model_families": {"A": "deepseek", "B": "other"}} | kw
    return check(client.post("/api/imports", json=payload), "/imports", "post")


def test_a_pi_import_is_a_completed_exploratory_run_with_its_trail(client):  # noqa: F811
    out = imported(client)
    assert out["created"] and out["trail_lines"] == 12 and out["events"]["posted"] == 3 and out["events"]["settled"] == 3
    run = check(client.get(f"/api/runs/{out['run_id']}"), "/runs/{run_id}")
    assert (run["source"], run["kind"], run["split"], run["status"], run["seed"]) == (
        "import",
        "exploratory",
        "exploratory",
        "completed",
        0,
    )
    assert run["models"] == ["deepseek-v4-flash", "other-model"] and run["t"] == 3
    listed = check(client.get("/api/runs"), "/runs")
    assert {r["id"]: r["source"] for r in listed["items"]}[out["run_id"]] == "import"
    events = check(client.get(f"/api/runs/{out['run_id']}/events", params={"types": "posted"}), "/runs/{run_id}/events")
    assert [e["text"] for e in events["events"]] == ["Hold 10.5?", "Yes.", "Keep it."]
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/runs/{out['run_id']}/export", params={"format": "zip"}).content))
    trail = [json.loads(x) for x in z.read("trail.jsonl").decode().splitlines()]
    assert len(trail) == 12 and trail[0]["source"] == "pi" and trail[0]["messages"][-1]["parts"][0]["part_kind"] == "thinking"
    assert client.app.state.db.ledger(out["run_id"]), "the run has its ledger line"


def test_the_same_files_again_return_the_stored_run_and_write_nothing(client):  # noqa: F811
    first = imported(client)
    lines = len(client.app.state.db.ledger())
    r = client.post("/api/imports", json={"format": "pi", "files": pi_files()})
    again = check(r, "/imports", "post")
    assert r.status_code == 200 and not again["created"] and again["run_id"] == first["run_id"]
    assert len(client.app.state.db.ledger()) == lines


def test_a_rows_csv_import_and_bad_input(client):  # noqa: F811
    out = check(client.post("/api/imports", json={"format": "rows", "files": {"log.csv": ROWS}}), "/imports", "post")
    run = check(client.get(f"/api/runs/{out['run_id']}"), "/runs/{run_id}")
    assert run["source"] == "import" and out["events"]["acted"] == 4 and out["trail_lines"] == 2
    bad = [
        ({"format": "rows", "files": {"log.csv": "speaker,round\nA B,1\n"}}, "files"),
        ({"format": "rows", "files": {"a.json": "[]", "b.json": "[]"}}, None),
        ({"format": "pi", "files": {"A": pi_files()["A"]}}, None),
        ({"format": "pi", "files": {"A": "{}", "B": "{}"}}, "files"),
        ({"format": "pi", "files": pi_files(), "model_families": {"C": "x"}}, "model_families"),
    ]
    for payload, path in bad:
        r = client.post("/api/imports", json=payload)
        problem = check(r, "/imports", "post")
        assert r.status_code == 422 and problem["code"] == "validation_error", (payload, r.text)
        if path:
            assert [e["path"] for e in problem["errors"]] == [path], r.text


def test_an_imported_run_cannot_be_branched(client):  # noqa: F811
    """CR-76 major 1: the stored spec is a valid RunSpec (the plan accepts the import's grants and channels), and the
    branch route refuses the import before it reads the spec."""
    out = imported(client)
    row = client.app.state.db.run_config(out["run_id"])
    spec = RunSpec.model_validate_json(row["spec_json"])
    assert {a.id: [g.tool_id for g in a.capability.grants] for a in spec.agents} == {
        "A": ["set_price", "hold", "post"],
        "B": ["set_price", "hold", "post"],
    }
    r = client.post(f"/api/runs/{out['run_id']}/branches", json={"at": 2, "rounds": 1, "n": 1, "intervention": {"type": "placebo"}})
    assert r.status_code == 409 and "imported log" in r.json()["message"]


def test_the_imported_block_judges_an_import_and_no_other_block_does(client, evals):  # noqa: F811
    out = imported(client)
    done = evals.wait(evals.create(body([out["run_id"]], block="imported"))["id"])
    assert done["status"] == "completed" and done["per_backend"]["scripted"]["counts"] == {"ok": 1}
    assert done["episodes"][0]["split"] == "exploratory"
    with pytest.raises(ApiError) as e:
        evals.create(body([out["run_id"]], block="wording"))
    assert e.value.errors[0]["message"] == "split_not_in_block"
    [sim] = pilot_runs(client, seeds=(1,))
    with pytest.raises(ApiError) as e:
        evals.create(body([sim], block="imported"))
    assert e.value.errors[0]["message"] == "split_not_in_block"


def test_the_judge_family_check_reads_the_import_families(client, evals):  # noqa: F811
    given, none = imported(client), imported(client, files={**pi_files(), "B": pi_files()["B"] + "\n"}, model_families={})
    assert evals._families(given["run_id"]) == {"deepseek", "other"}
    assert evals._families(none["run_id"]) == {None}


def test_the_imported_block_is_outside_the_plan_cap_and_capped_per_job(client):  # noqa: F811
    blocks = {b["id"]: b for b in check(client.get("/api/judge/blocks"), "/judge/blocks")}
    assert not blocks["imported"]["in_plan"] and blocks["imported"]["splits"] == ["exploratory"]
    assert all(b["in_plan"] for k, b in blocks.items() if k != "imported")
    assert CAP_PER_BACKEND == 280, "the plan's cap does not count the imported block"
    ledger = check(client.get("/api/judge/ledger"), "/judge/ledger")
    assert ledger["cap_per_backend"] == 280

    def calls(block, n):
        return [PlannedCall(i, f"run_{i}", "dev", "talk_only", "none", "llm", 1, block) for i in range(n)]

    full = LedgerState(first={("llm", "imported"): 10_000}, retries={}, held=calls("imported", 40))
    assert batch.over_cap(batch.room(full, calls("main", 10))) is None, "imported use leaves the plan's room alone"
    plan_full = LedgerState(first={("llm", "main"): 180, ("llm", "talk_prices"): 30, ("llm", "wording"): 20, ("llm", "stability"): 10,
                                   ("llm", "robustness"): 10}, retries={}, held=[])  # fmt: skip
    assert batch.over_cap(batch.room(plan_full, calls("imported", BLOCKS["imported"].cap_per_backend))) is None
    assert "per job" in batch.over_cap(batch.room(plan_full, calls("imported", BLOCKS["imported"].cap_per_backend + 1)))


def test_the_imported_trail_lines_match_the_trail_line_schema(client):  # noqa: F811
    """CR-76 minor 3: seq, request_hash, provider_profile_id and model may be null, and `source` is in the contract."""
    spec = yaml.safe_load(Path("docs/sim/api/openapi.yaml").read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    validator = jsonschema.Draft202012Validator({**schemas["TrailLine"], "components": {"schemas": schemas}})
    for payload in [{"format": "pi", "files": pi_files()}, {"format": "rows", "files": {"log.csv": ROWS}}]:
        out = check(client.post("/api/imports", json=payload), "/imports", "post")
        z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/runs/{out['run_id']}/export", params={"format": "zip"}).content))
        lines = [json.loads(x) for x in z.read("trail.jsonl").decode().splitlines()]
        assert lines and all(line["seq"] is None for line in lines)
        for line in lines:
            validator.validate(line)


def test_bad_numbers_are_422(client):  # noqa: F811
    """CR-76 major 2 and 3, through the route."""
    for log in [
        "speaker,round,action\nA,1,nan\nB,1,5\n",
        "speaker,round,action\nA,1,-1\nB,1,5\n",
        "speaker,round,action\nA,1001,5\nB,1,5\n",
    ]:
        r = client.post("/api/imports", json={"format": "rows", "files": {"log.csv": log}})
        assert r.status_code == 422 and check(r, "/imports", "post")["code"] == "validation_error", r.text


def test_two_imports_of_the_same_files_at_once_store_one_run(client, monkeypatch):  # noqa: F811
    """CR-76 minor 1: the run-id lock. A slow finish_run keeps the first import open while the second one checks."""
    finish = imports.finish_run

    def slow(*args):
        time.sleep(0.3)
        return finish(*args)

    monkeypatch.setattr(imports, "finish_run", slow)
    app = client.app.state
    body = ImportCreate.model_validate({"format": "pi", "files": pi_files()})
    results = []

    def one():
        results.append(imports.create(app.db, app.svc.writer, app.paths.runs, body, app.svc.now())[0])

    threads = [threading.Thread(target=one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    (created,) = [r for r in results if r]
    assert sorted(results) == [False, True] and len(app.db.ledger(imports._parse(body, "").run_id)) == 1


def test_an_import_that_stopped_before_its_ledger_line_is_stored_by_the_next_one(client, monkeypatch):  # noqa: F811
    """CR-76 minor 1: config and events are in, the ledger line is not; the same files again finish it, with one line."""

    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(imports, "finish_run", broken)
    app = client.app.state
    body = ImportCreate.model_validate({"format": "pi", "files": pi_files()})
    with pytest.raises(OSError):
        imports.create(app.db, app.svc.writer, app.paths.runs, body, app.svc.now())
    run_id = imports._parse(body, "").run_id
    assert app.db.run_config(run_id) is not None and not app.db.ledger(run_id)
    monkeypatch.undo()
    out = check(client.post("/api/imports", json={"format": "pi", "files": pi_files()}), "/imports", "post")
    assert out["created"] and len(app.db.ledger(run_id)) == 1
    assert check(client.get(f"/api/runs/{run_id}"), "/runs/{run_id}")["status"] == "completed"


def test_an_import_has_its_run_folder_with_the_zips_bytes(client):  # noqa: F811
    """CR-86 minor 4 (case 11): an import is stored without RunService.execute, so imports.create writes the folder."""
    from test_run_folder import BUNDLE, manifest, sha, zipped

    rid = imported(client)["run_id"]
    here = client.app.state.svc.runs_dir / rid
    assert sorted(p.name for p in here.iterdir()) == sorted([*BUNDLE, "manifest.json"])
    m = manifest(here)
    assert (m["manifest"], m["run_id"], [f["path"] for f in m["files"]]) == ("run-folder.v1", rid, BUNDLE)
    z = zipped(client, f"/api/runs/{rid}/export")
    for f in m["files"]:
        data = (here / f["path"]).read_bytes()
        assert data == z.read(f["path"]) and f["sha256"] == sha(data), f["path"]
    assert json.loads((here / "meta.json").read_text(encoding="utf-8"))["source"] == "import"


def test_a_failed_folder_write_keeps_the_import_and_the_same_files_again_write_it(client, monkeypatch):  # noqa: F811
    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(imports, "write_run_folder", broken)
    first = imported(client)
    folder = client.app.state.svc.runs_dir / first["run_id"]
    assert first["created"] and client.app.state.db.ledger(first["run_id"]), "the import is stored"
    assert not (folder / "manifest.json").exists()
    monkeypatch.undo()
    again = check(client.post("/api/imports", json={"format": "pi", "files": pi_files()}), "/imports", "post")
    assert not again["created"] and (folder / "manifest.json").is_file()
