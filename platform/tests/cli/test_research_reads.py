"""Research setup: conditions, seeds and freeze state are shown as the server gives them; `freeze run` is the one write (POST /freeze)."""

import json
import shutil
import sqlite3
from contextlib import closing

import httpx2
import pytest
import yaml
from conftest import ROOT
from fastapi.testclient import TestClient
from test_cli_evaluations import RUBRIC

from api.app import create_app
from services.profiles import ProfileRecord
from simcli.main import cli
from condcopy import draft_conditions


def test_conditions_list_has_every_condition_file(server):
    r = server("conditions", "list")
    ids = {p.stem for p in (ROOT / "conditions").glob("*.yaml")} - {"prompts.draft", "seeds", "judge-rubric", "models", "decision-rule"}
    assert r.exit_code == 0 and ids and all(f"\n{i} " in r.stdout for i in ids)
    assert server("conditions", "list", "--json").json == server.http.get("conditions").json()


def test_conditions_show_is_the_api_body_unchanged(server):
    r = server("conditions", "show", "C1", "--json")
    assert r.exit_code == 0 and r.json == server.http.get("conditions/C1").json()
    assert {"labels", "hash", "capabilities", "prompt_variants"} <= set(r.json)


def test_an_unknown_condition_is_not_found(server):
    r = server("conditions", "show", "C9", "--json")
    assert r.exit_code == 1 and r.json["code"] == "not_found" and r.json["status"] == 404


def test_seeds_list_shows_each_split_and_the_test_lock(server):
    body = server.http.get("seeds").json()
    r = server("seeds", "list")
    assert r.exit_code == 0
    for split in ("pilot", "dev", "test"):
        line = next(x for x in r.stdout.splitlines() if x.startswith(split))
        assert line.split()[1] == str(len(body[split])) and ", ".join(map(str, body[split])) in line
    assert body["hash"] in r.stdout and "test_locked  True" in r.stdout


def test_seeds_list_shows_each_seed_and_the_design_balance(server):
    entries = server.http.get("seeds").json()["seeds"]
    r = server("seeds", "list")
    for e in entries:
        row = next(x.split() for x in r.stdout.splitlines() if x.split()[:2] == [str(e["seed"]), e["split"]])
        assert row[2] == e["shock_news"] and e["scenario_id"] in row and (e["informed_seller"] is None or e["informed_seller"] in row)
    for split in ("pilot", "dev", "test"):
        mine = [e for e in entries if e["split"] == split]
        private = sum(e["shock_news"] == "private" for e in mine)
        line = next(x for x in r.stdout.splitlines() if x.startswith(split))
        assert f"public {len(mine) - private}, private {private}" in line


def test_seeds_list_without_entries_keeps_the_split_table(run_cli):
    body = {"hash": "sha256:1", "pilot": [1], "dev": [101], "test": [201], "test_locked": True}
    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: httpx2.Response(200, json=body)))
    r = run_cli(http, "seeds", "list")
    assert r.exit_code == 0 and "balance" not in r.stdout and "pilot  1  1" in r.stdout


def test_no_seed_file_gives_seeds_missing(tmp_path, run_cli):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions", "seeds.yaml")
    with TestClient(create_app(tmp_path, use_dbos=False), base_url="http://testserver/api") as http:
        r = run_cli(http, "seeds", "list", "--json")
    assert r.exit_code == 1 and r.json["code"] == "seeds_missing"


def test_freeze_status_after_a_freeze_and_after_a_change(tmp_path, run_cli):
    frozen = research_root(tmp_path)
    with TestClient(create_app(tmp_path, use_dbos=False), base_url="http://testserver/api") as http:
        for stage in ("conditions", "rule"):
            assert run_cli(http, "freeze", "run", "--stage", stage, "--approved-by", "lead", "--json").exit_code == 0
        ok = run_cli(http, "freeze", "status", "--json")
        served = http.get("freeze").json()
        record = json.loads(frozen.read_text(encoding="utf-8"))
        for st in record["stages"].values():
            st["hashes"]["seeds"] = "sha256:0"
        frozen.write_text(json.dumps(record), encoding="utf-8")
        changed = run_cli(http, "freeze", "status", "--json")
        human = run_cli(http, "freeze", "status")
    assert ok.exit_code == 0 and ok.json == served
    assert (ok.json["frozen"], ok.json["valid"], ok.json["problems"]) == (True, True, [])
    assert changed.exit_code == 0 and changed.json["valid"] is False and "seeds changed since the freeze" in changed.json["problems"]
    assert "seeds changed since the freeze" in human.stdout


def test_freeze_status_with_no_frozen_file(server):
    r = server("freeze", "status", "--json")
    assert r.exit_code == 0 and r.json["frozen"] is False and r.json["valid"] is False
    assert "frozen.json not found" in server("freeze", "status").stdout


def test_research_reads_send_only_gets_and_encode_the_id(run_cli):
    sent = []
    reply = httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(200, json={}))
    http = httpx2.Client(base_url="http://fake/api", transport=reply)
    run_cli(http, "conditions", "show", "a/b?x", "--json")
    assert sent[0].method == "GET" and sent[0].url.raw_path == b"/api/conditions/a%2Fb%3Fx"
    assert run_cli(http, "conditions", "show", "..").exit_code == 2


def test_no_research_command_can_write_except_freeze_run():
    commands = {(g, n): c for g in ("conditions", "seeds", "freeze") for n, c in cli.commands[g].commands.items()}
    assert set(commands) == {("conditions", "list"), ("conditions", "show"), ("seeds", "list"), ("freeze", "status"), ("freeze", "run")}
    reads = [c for key, c in commands.items() if key != ("freeze", "run")]
    assert not any(p.name in ("dry_run", "allow_paid", "file", "config") for c in reads for p in c.params)
    assert {p.name for p in commands["freeze", "run"].params} >= {"stage", "approved_by", "dry_run"}


# freeze run (POST /freeze, DECISIONS-11 §3): a one-line wrapper


MODELS = {  # scripted sellers and a judge entry with its profile fields (CR-43 M1)
    "id": "models-test",
    "sellers": {"A": "scripted", "B": "scripted"},
    "judge": {"provider_profile_id": "prof_judge", "model": "m-judge", "model_family": "judge-fam",
              "base_url": "https://judge.invalid/v1", "protocol": "openai_chat"},
}  # fmt: skip
RESEARCH_FILES = {
    "decision-rule.yaml": "rule: corr_lag > 0.4\n",
    "judge-rubric.yaml": yaml.safe_dump(RUBRIC, sort_keys=False),  # a valid item set: stage conditions parses it
    "models.yaml": yaml.safe_dump(MODELS, sort_keys=False),
}


def research_root(tmp_path, files=tuple(RESEARCH_FILES)):
    """A copy of the repo's scenarios and conditions, plus the named research files. The judge rubric freezes with stage
    conditions; the decision rule with stage rule (contract 0.13.0, item 3 L1)."""
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    for name in files:
        (tmp_path / "conditions" / name).write_text(RESEARCH_FILES[name], encoding="utf-8")
    if "models.yaml" in files:  # the judge's profile, without a key: the freeze compares each entry with it (CR-47 m2)
        judge = MODELS["judge"]
        rec = ProfileRecord(
            id=judge["provider_profile_id"], name="Judge (test)", base_url=judge["base_url"], created_at="2026-10-22T00:00:00Z"
        )
        (tmp_path / "data").mkdir(exist_ok=True)
        with closing(sqlite3.connect(tmp_path / "data" / "profiles.sqlite")) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, json TEXT NOT NULL)")
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?)", (rec.id, rec.model_dump_json()))
    return tmp_path / "conditions" / "frozen.json"


def test_freeze_run_conditions_opens_dev_then_rule_opens_test(tmp_path, run_cli):
    frozen = research_root(tmp_path)
    with TestClient(create_app(tmp_path, use_dbos=False), base_url="http://testserver/api") as http:
        cond = run_cli(http, "freeze", "run", "--stage", "conditions", "--approved-by", "lead", "--json")
        after_cond = run_cli(http, "freeze", "status", "--json").json
        rule = run_cli(http, "freeze", "run", "--stage", "rule", "--approved-by", "lead", "--json")
        status = run_cli(http, "freeze", "status", "--json")
        served = http.get("freeze").json()
    assert cond.exit_code == 0 and after_cond["stages"]["conditions"]["valid"] is True
    assert after_cond["valid"] is False and after_cond["stages"]["rule"]["frozen"] is False
    assert rule.exit_code == 0 and rule.json["valid"] is True and rule.json["problems"] == []
    record = json.loads(frozen.read_text(encoding="utf-8"))["stages"]
    assert record["rule"]["approved_by"] == "lead" and rule.json["hashes"] == record["rule"]["hashes"]
    assert {"seeds", "features", "judge_rubric"} <= set(record["conditions"]["hashes"])
    assert "decision_rule" not in record["conditions"]["hashes"]
    assert {"seeds", "features", "decision_rule", "judge_rubric"} <= set(record["rule"]["hashes"])
    assert status.json == served and status.json["valid"] is True and status.json["stages"]["conditions"]["valid"] is True


def test_freeze_run_rule_before_conditions_is_a_conflict_and_writes_nothing(tmp_path, run_cli):
    frozen = research_root(tmp_path)
    before = {f.name: f.read_bytes() for f in (tmp_path / "conditions").glob("*.yaml")}
    with TestClient(create_app(tmp_path, use_dbos=False), base_url="http://testserver/api") as http:
        r = run_cli(http, "freeze", "run", "--stage", "rule", "--approved-by", "lead", "--json")
    assert r.exit_code == 1 and r.json["code"] == "conflict" and not frozen.exists()
    assert before == {f.name: f.read_bytes() for f in (tmp_path / "conditions").glob("*.yaml")}


def test_freeze_run_conditions_without_the_judge_rubric_is_freeze_incomplete(tmp_path, run_cli):
    frozen = research_root(tmp_path, files=("decision-rule.yaml",))
    with TestClient(create_app(tmp_path, use_dbos=False), base_url="http://testserver/api") as http:
        r = run_cli(http, "freeze", "run", "--stage", "conditions", "--approved-by", "lead", "--json")
    assert r.exit_code == 1 and r.json["code"] == "freeze_incomplete" and "judge-rubric.yaml" in r.json["message"]
    assert not frozen.exists()


def test_freeze_run_rule_without_the_decision_rule_is_freeze_incomplete(tmp_path, run_cli):
    frozen = research_root(tmp_path, files=("judge-rubric.yaml", "models.yaml"))
    with TestClient(create_app(tmp_path, use_dbos=False), base_url="http://testserver/api") as http:
        assert run_cli(http, "freeze", "run", "--stage", "conditions", "--approved-by", "lead", "--json").exit_code == 0
        stored = frozen.read_bytes()
        r = run_cli(http, "freeze", "run", "--stage", "rule", "--approved-by", "lead", "--json")
    assert r.exit_code == 1 and r.json["code"] == "freeze_incomplete" and "decision-rule.yaml" in r.json["message"]
    assert "judge-rubric.yaml" not in r.json["message"] and frozen.read_bytes() == stored


def test_freeze_run_sends_one_post_with_the_stage_and_approver_and_dry_run_sends_nothing(run_cli):
    sent = []
    body = {"frozen": True, "valid": True, "problems": [], "stages": {}, "hashes": {"seeds": "sha256:1"}}
    http = httpx2.Client(
        base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(200, json=body))
    )
    dry = run_cli(http, "freeze", "run", "--stage", "rule", "--approved-by", "lead", "--dry-run", "--json")
    assert dry.exit_code == 0 and dry.json["dry_run"] is True and sent == []
    r = run_cli(http, "freeze", "run", "--stage", "rule", "--approved-by", "lead", "--json")
    assert r.json == body and [(q.method, q.url.path, json.loads(q.content)) for q in sent] == [
        ("POST", "/api/freeze", {"stage": "rule", "approved_by": "lead"})
    ]


@pytest.mark.parametrize(
    "args",
    [["--stage", "rule"], ["--approved-by", "lead"], ["--stage", "test", "--approved-by", "lead"]],
    ids=["no approver", "no stage", "unknown stage"],
)
def test_freeze_run_needs_a_stage_and_an_approver(run_cli, args):
    http = httpx2.Client(base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: pytest.fail("sent a request")))
    assert run_cli(http, "freeze", "run", *args, "--json").exit_code == 2
