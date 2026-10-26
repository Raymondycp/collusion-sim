"""Each command's --json output validates against the contract schema of its path (CLI-DESIGN §4.2)."""

import pytest
import yaml
from conftest import ROOT
from jsonschema import Draft202012Validator

SPEC = yaml.safe_load((ROOT / "docs" / "sim" / "api" / "openapi.yaml").read_text(encoding="utf-8"))


def schema(path: str) -> dict:
    content = SPEC["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
    return {**content["schema"], "components": SPEC["components"]}


CASES = [
    (["health"], "/health"),
    (["demand-models", "list"], "/demand-models"),
    (["roles", "list"], "/roles"),
    (["tools", "list"], "/tools"),
    (["policies", "list"], "/policies"),
    (["generators", "list"], "/scenario-generators"),
    (["indicators", "list"], "/indicators"),
    (["ledger", "list"], "/ledger"),
    (["ledger", "verify"], "/ledger/verify"),
    (["runs", "list"], "/runs"),
    (["runs", "show", "{run}"], "/runs/{run_id}"),
    (["runs", "events", "{run}", "--limit", "40"], "/runs/{run_id}/events"),
    (["runs", "series", "{run}"], "/runs/{run_id}/series"),
    (["runs", "state", "{run}", "--t", "5"], "/runs/{run_id}/state"),
    (["runs", "observations", "{run}", "--agent", "B", "--t", "5"], "/runs/{run_id}/observations"),
    (["branches", "list", "{run}"], "/runs/{run_id}/branches"),
    (["indicators", "series", "{run}", "delta_p"], "/runs/{run_id}/indicators/{indicator_id}"),
    (["conditions", "list"], "/conditions"),
    (["conditions", "show", "C1"], "/conditions/{condition_id}"),
    (["seeds", "list"], "/seeds"),
    (["freeze", "status"], "/freeze"),
]


@pytest.mark.parametrize("args, path", CASES, ids=[" ".join(a[:2]) for a, _ in CASES])
def test_json_output_matches_the_contract(server, args, path):
    r = server(*[a.replace("{run}", server.run_ids[0]) for a in args], "--json")
    assert r.exit_code == 0
    Draft202012Validator(schema(path)).validate(r.json)
