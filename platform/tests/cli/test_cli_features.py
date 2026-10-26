"""Unit 8 (contract 0.11.0): `features export` writes the experiment's feature table unchanged, with features_code_hash."""

import csv
import hashlib
import io
import json
import shutil

import httpx2
import pytest
import yaml
from click.testing import CliRunner
from conftest import ROOT, Result
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.app import create_app
from simcli.main import cli

SPEC = yaml.safe_load((ROOT / "docs" / "sim" / "api" / "openapi.yaml").read_text(encoding="utf-8"))
SELLERS = [
    {"id": "A", "role": "seller", "policy": {"kind": "price_schedule", "params": {"prices": {}, "default": 5.75}}},
    {"id": "B", "role": "seller", "policy": {"kind": "price_schedule", "params": {"prices": {}, "default": 5.75}}},
]


def run(http, *args: str) -> Result:
    r = CliRunner().invoke(cli, list(args), obj={"http": http}, catch_exceptions=False)
    return Result(r.exit_code, r.stdout, r.stderr)


@pytest.fixture(scope="module")
def done(tmp_path_factory):
    """One finished scripted experiment: C1, seed 1. Two constant sellers, so corr_raw and corr_lag are null (flat_prices)."""
    root = tmp_path_factory.mktemp("featroot")
    for folder in ("scenarios", "conditions"):
        shutil.copytree(ROOT / folder, root / folder)
    with TestClient(create_app(root, use_dbos=False), base_url="http://testserver/api") as http:
        body = {
            "conditions": ["C1"],
            "split": "pilot",
            "seeds": [1],
            "agents": SELLERS,
            "budget": {"max_usd": 1.0},
            "batch_budget": {"max_calls": 100},
        }
        r = http.post("experiments", json=body)
        assert r.status_code < 300, r.text
        exp_id = r.json()["id"]
        assert http.app.state.experiments.wait(exp_id, timeout=300)["status"] == "completed"
        yield http, exp_id


def test_json_export_is_the_server_bytes_with_the_code_hash(done, tmp_path):
    http, exp_id = done
    out = tmp_path / "features.json"
    r = run(http, "features", "export", exp_id, "-o", str(out), "--json")
    assert r.exit_code == 0, r.stderr
    served = http.get(f"experiments/{exp_id}/features").content
    assert out.read_bytes() == served
    table = json.loads(served)
    Draft202012Validator({"$ref": "#/components/schemas/FeatureTable", "components": SPEC["components"]}).validate(table)
    assert r.json["features_code_hash"] == table["meta"]["features_code_hash"] != ""
    assert r.json["sha256"] == "sha256:" + hashlib.sha256(served).hexdigest()
    assert (r.json["rows"], r.json["layer"], r.json["format"]) == (1, "B", "json")


def test_a_null_feature_stays_null_with_its_reason(done, tmp_path):
    http, exp_id = done
    out = tmp_path / "f.json"
    run(http, "features", "export", exp_id, "-o", str(out))
    row = json.loads(out.read_text(encoding="utf-8"))["rows"][0]
    for k in ("corr_raw", "corr_lag"):
        assert (row[k], row[f"{k}_missing"]) == (None, "flat_prices"), k


def test_csv_export_keeps_empty_cells_and_layer_a_has_fewer_features(done, tmp_path):
    http, exp_id = done
    out = tmp_path / "a.csv"
    r = run(http, "features", "export", exp_id, "--layer", "A", "--format", "csv", "-o", str(out), "--json")
    assert r.exit_code == 0
    assert out.read_bytes() == http.get(f"experiments/{exp_id}/features", params={"layer": "A", "format": "csv"}).content
    row = next(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert row["corr_lag"] == "" and row["corr_lag_missing"] == "flat_prices"
    b = run(http, "features", "export", exp_id, "-o", str(tmp_path / "b.json"), "--json").json
    assert r.json["features"] < b["features"] and r.json["features_code_hash"] == b["features_code_hash"]


def test_csv_export_is_one_request_and_writes_the_meta_beside_the_file(done, tmp_path):
    """CR-25 m2: the csv, the summary and the per-feature versions and hashes all come from one response."""
    http, exp_id = done
    served = http.get(f"experiments/{exp_id}/features").content
    sent = []
    fake = httpx2.Client(
        base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(200, content=served))
    )
    out = tmp_path / "t.csv"
    r = run(fake, "features", "export", exp_id, "--format", "csv", "-o", str(out), "--json")
    assert r.exit_code == 0 and [q.url.params["format"] for q in sent] == ["json"]
    assert out.read_bytes() == http.get(f"experiments/{exp_id}/features", params={"format": "csv"}).content
    meta = json.loads((tmp_path / "t.csv.meta.json").read_text(encoding="utf-8"))
    assert meta == json.loads(served)["meta"] and r.json["meta_path"] == str(tmp_path / "t.csv.meta.json")
    assert all({"id", "version", "data_layer", "code_hash"} <= set(f) for f in meta["features"])


def test_an_existing_meta_file_is_refused_without_overwrite(done, tmp_path):
    http, exp_id = done
    (tmp_path / "m.csv.meta.json").write_text("keep", encoding="utf-8")
    r = run(http, "features", "export", exp_id, "--format", "csv", "-o", str(tmp_path / "m.csv"), "--json")
    assert r.json["code"] == "path_refused" and not (tmp_path / "m.csv").exists()
    r = run(http, "features", "export", exp_id, "--format", "csv", "-o", str(tmp_path / "m.csv"), "--overwrite", "--json")
    assert r.exit_code == 0 and (tmp_path / "m.csv.meta.json").read_text(encoding="utf-8") != "keep"


def test_two_exports_are_byte_identical(done, tmp_path):
    http, exp_id = done
    first = run(http, "features", "export", exp_id, "-o", str(tmp_path / "1.json"), "--json").json
    second = run(http, "features", "export", exp_id, "-o", str(tmp_path / "2.json"), "--json").json
    assert first["sha256"] == second["sha256"]


def test_the_human_summary_names_the_file_and_the_hash(done, tmp_path):
    http, exp_id = done
    out = run(http, "features", "export", exp_id, "-o", str(tmp_path / "h.json")).stdout
    lines = {x.split()[0]: x.split()[1:] for x in out.splitlines() if x.split()}
    assert lines["rows"] == ["1"] and lines["features_code_hash"][0] and lines["path"]


@pytest.mark.parametrize("status, code", [(404, "not_found"), (409, "test_split_locked")])
def test_a_problem_passes_through_and_writes_no_file(tmp_path, status, code):
    sent = []
    problem = {"type": "about:blank", "title": "x", "status": status, "code": code, "message": "m"}
    http = httpx2.Client(
        base_url="http://fake/api",
        transport=httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(status, json=problem)),
    )
    out = tmp_path / "x.json"
    r = run(http, "features", "export", "exp_00000000000000000000000000", "-o", str(out), "--json")
    assert r.exit_code == 1 and r.json["code"] == code and not out.exists()
    assert [(q.method, q.url.path, q.url.params["format"]) for q in sent] == [
        ("GET", "/api/experiments/exp_00000000000000000000000000/features", "json")
    ]


def test_an_existing_file_or_a_server_folder_is_refused_before_any_request(tmp_path, monkeypatch):
    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: pytest.fail("sent a request")))
    existing = tmp_path / "keep.json"
    existing.write_bytes(b"keep")
    r = run(http, "features", "export", "exp_x", "-o", str(existing), "--json")
    assert r.json["code"] == "path_refused" and existing.read_bytes() == b"keep"
    monkeypatch.setenv("SIM_ROOT", str(tmp_path))
    r = run(http, "features", "export", "exp_x", "-o", str(tmp_path / "runs" / "f.json"), "--json")
    assert r.json["code"] == "path_refused" and not (tmp_path / "runs").exists()


def test_export_has_no_write_options():
    params = {p.name for p in cli.commands["features"].commands["export"].params}
    assert not params & {"allow_paid", "dry_run"}


def test_each_evaluation_is_passed_as_evaluation_id_and_an_unknown_one_writes_no_file(done, tmp_path):
    http, exp_id = done
    out = tmp_path / "judged.json"
    r = run(http, "features", "export", exp_id, "--evaluation", "eval_nope", "-o", str(out), "--json")
    assert (r.exit_code, r.json["code"], out.exists()) == (1, "not_found", False)
    served, sent = http.get(f"experiments/{exp_id}/features").content, []
    fake = httpx2.Client(
        base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(200, content=served))
    )
    r = run(fake, "features", "export", exp_id, "--evaluation", "eval_1", "--evaluation", "eval_2", "-o", str(out), "--json")
    assert r.exit_code == 0 and r.json["evaluation_ids"] == ["eval_1", "eval_2"] and out.read_bytes() == served
    assert sent[0].url.params.multi_items() == [
        ("layer", "B"),
        ("format", "json"),
        ("evaluation_id", "eval_1"),
        ("evaluation_id", "eval_2"),
    ]
    assert "evaluation_ids" not in run(fake, "features", "export", exp_id, "-o", str(tmp_path / "plain.json"), "--json").json
