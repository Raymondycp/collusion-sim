"""Registries and the ledger: --json equals the API body (CLI-DESIGN §3.1, §3.2)."""

import json

import httpx2
import pytest


@pytest.mark.parametrize(
    "args, path",
    [
        (["health"], "health"),
        (["demand-models", "list"], "demand-models"),
        (["roles", "list"], "roles"),
        (["tools", "list"], "tools"),
        (["policies", "list"], "policies"),
        (["generators", "list"], "scenario-generators"),
        (["indicators", "list"], "indicators"),
        (["ledger", "list"], "ledger"),
        (["ledger", "verify"], "ledger/verify"),
    ],
)
def test_list_commands_print_the_api_body(server, args, path):
    r = server(*args, "--json")
    assert r.exit_code == 0 and r.json == server.http.get(path).json()
    assert server(*args).exit_code == 0


def test_show_picks_one_entry_and_refuses_an_unknown_id(server):
    linear = server("demand-models", "show", "linear", "--json").json
    assert linear == next(m for m in server.http.get("demand-models").json() if m["id"] == "linear")
    assert server("roles", "show", "seller", "--json").json["name"] == "seller"
    missing = server("tools", "show", "no_such_tool", "--json")
    assert missing.exit_code == 1 and missing.json["code"] == "not_found"


def test_ledger_verify_covers_every_run(server):
    assert server("ledger", "verify", "--json").json == {"ok": True, "entries": 4, "first_bad_line": None}


def test_demand_solve_sends_the_targets_file(server, tmp_path):
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({"nash": 5.75, "joint_optimum": 10.5, "b": 1.5, "c": 1.0}), encoding="utf-8")
    solved = server("demand-models", "solve", "linear", "--targets", str(targets), "--json").json
    assert solved["params"]["a"] == 10 and solved["params"]["d"] == 1 and solved["problems"] == []


def test_a_targets_file_that_is_not_json_sends_no_request(run_cli, tmp_path):

    sent = []
    http = httpx2.Client(
        base_url="http://fake/api", transport=httpx2.MockTransport(lambda req: sent.append(req) or httpx2.Response(200, json={}))
    )
    bad = tmp_path / "targets.json"
    bad.write_text("{nash: 5", encoding="utf-8")
    r = run_cli(http, "demand-models", "solve", "linear", "--targets", str(bad), "--json")
    assert r.exit_code == 1 and r.json["code"] == "file_invalid" and sent == []


def test_a_missing_targets_file_is_file_invalid(server, tmp_path):
    r = server("demand-models", "solve", "linear", "--targets", str(tmp_path / "none.json"), "--json")
    assert r.exit_code == 1 and r.json["code"] == "file_invalid"
