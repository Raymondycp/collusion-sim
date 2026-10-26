"""scripts/plan_estimate.py (acceptance A6) against the in-process app, with fake tokens per call. No LLM call."""

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api.app import create_app
from services import seeds as seeds_svc
from test_profiles import MemoryKeyring

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import plan_estimate as pe  # noqa: E402
import v1_smoke as smoke  # noqa: E402

FAKE = ["--tokens-per-call", "3000,200", "--judge-tokens", "8000,400", "--price", "1,4", "--budget-usd", "100"]


@pytest.fixture
def server(tmp_path):
    """A root with the repo's conditions and seeds, one fake profile and a models.yaml whose sellers use it."""
    for d in ("conditions", "scenarios"):
        shutil.copytree(ROOT / d, tmp_path / d, ignore=shutil.ignore_patterns("frozen.json"))
    (tmp_path / "conditions" / "judge-rubric.yaml").write_text(json.dumps(smoke.RUBRIC), encoding="utf-8")
    with TestClient(create_app(tmp_path, use_dbos=False, key_backend=MemoryKeyring())) as c:
        profile = {"name": "fake", "base_url": "https://llm.example.test/v1", "api_key": "sk-fake-plan-0000", "default_model": "m"}
        r = c.post("/api/provider-profiles", json=profile)
        assert r.status_code == 201, r.text
        prof = r.json()["id"]
        sampling = {
            "temperature": 0.7,
            "top_p": None,
            "max_output_tokens": 500,
            "timeout_s": 60,
            "thinking": None,
            "thinking_on_timeout": None,
        }
        seller = {"provider_profile_id": prof, "model": "m", "model_family": "fam", **sampling}
        models = {"id": "models-plan", "sellers": {"A": seller, "B": seller},
                  "by_condition": {"C2b": {"B": seller | {"model": "m2", "model_family": "fam2"}}},  # C2b: a family each
                  "judge": {"provider_profile_id": prof, "model": "j", "model_family": "judge-fam"}}  # fmt: skip
        r = c.put("/api/research/models", json={"text": json.dumps(models)})
        assert r.status_code < 300, r.text
        yield c, tmp_path, prof


def main(c, capsys, *extra) -> str:
    def api(method, path, body):
        r = c.request(method, "/api" + path, json=body)
        return r.status_code, r.json()

    assert pe.main([*FAKE, *extra], api=api) == 0
    return capsys.readouterr().out


def run(c, capsys, *extra):
    return {r["condition_id"]: r for r in json.loads(main(c, capsys, "--json", *extra))}


def test_before_the_freeze_both_splits_use_the_mean_rounds_and_d13_counts(server, capsys):
    c, _, _ = server
    rows = run(c, capsys)
    c1, c2a, total = rows["C1"], rows["C2a"], rows["total"]
    assert c1["assumed"] == ["dev", "test"] and total["assumed"] == ["dev", "test"]
    # per cell: parent 30 + probe 5 + 3 placebo x 5 + ablation (C1 and C3 only) rounds 10..30 = 21
    assert (c1["cells"], c1["runs"], c1["run_rounds"]) == (20, 20 * 6, 20 * (30 + 5 + 15 + 21))
    assert (c2a["cells"], c2a["runs"], c2a["run_rounds"]) == (20, 20 * 5, 20 * (30 + 5 + 15))
    assert (c1["talk_per_round"], c1["act_per_round"]) == (2, 2)  # 2 sellers; a talk call only where the channel is open
    assert (c2a["talk_per_round"], c2a["act_per_round"]) == (0, 2)
    assert c1["calls_d13"] == 4 * c1["run_rounds"] and c1["calls"] == round(1.1 * c1["calls_d13"])
    assert c1["input_tokens"] == round(c1["calls"] * 3000) and c1["output_tokens"] == round(c1["calls"] * 200)
    assert c1["usd"] == round((c1["input_tokens"] + 4 * c1["output_tokens"]) / 1e6, 2)
    assert total["calls"] == sum(r["calls"] for k, r in rows.items() if k != "total")  # sellers, judge and checks in one total
    assert all(rows[k]["server_calls_max"] > rows[k]["calls"] for k in pe.CONDITIONS), (
        "the server's worst case counts every step and attempt"
    )


def test_after_the_conditions_freeze_dev_uses_each_seed_stop_round(server, capsys):
    c, root, _ = server
    r = c.post("/api/freeze", json={"stage": "conditions", "approved_by": smoke.APPROVER})
    assert r.status_code == 200, r.text
    rows = run(c, capsys)
    assert rows["C1"]["assumed"] == ["test"]
    stops = [x["stop_round"] for x in seeds_svc.rows(root) if x["split"] == "dev"]
    assert rows["C1"]["run_rounds"] == sum(s + 5 + 15 + (s - 10 + 1) for s in stops) + 10 * (30 + 5 + 15 + 21)


def test_a_seller_model_other_than_the_frozen_one_stops_the_script(server, capsys):
    c, _, prof = server
    assert c.post("/api/freeze", json={"stage": "conditions", "approved_by": smoke.APPROVER}).status_code == 200
    with pytest.raises(SystemExit, match="model_not_frozen"):
        main(c, capsys, "--profile", prof, "--model", "other")


def test_text_output_names_the_budget_and_the_assumption(server, capsys):
    c, _, _ = server
    out = main(c, capsys).splitlines()
    assert out[0].startswith("condition_id | cells | runs") and any(x.startswith("total |") for x in out)
    assert any(x.startswith("assumed: the server keeps split(s) dev, test closed") for x in out)
    assert any(x.startswith("budget: ") and x.endswith(("fits", "DOES NOT FIT")) for x in out)


def test_talk_and_act_tokens_count_apart(server, capsys):
    c, _, _ = server
    rows = run(c, capsys, "--talk-tokens", "5000,300", "--act-tokens", "2000,50")
    c1, c2a = rows["C1"], rows["C2a"]
    assert c1["talk_calls"] == c1["act_calls"] and c2a["talk_calls"] == 0  # C1: both sellers talk; C2a: no channel
    assert c1["talk_calls"] + c1["act_calls"] == c1["calls"]
    assert c1["input_tokens"] == round(c1["talk_calls"] * 5000 + c1["act_calls"] * 2000)
    assert c1["output_tokens"] == round(c1["talk_calls"] * 300 + c1["act_calls"] * 50)
    assert c2a["input_tokens"] == round(c2a["act_calls"] * 2000)
    per_call = run(c, capsys, "--tokens-per-call", "3000,200")["C1"]
    assert (per_call["talk_calls"], per_call["input_tokens"]) == (c1["talk_calls"], round(c1["calls"] * 3000))


def test_tokens_are_required(capsys):
    with pytest.raises(SystemExit):
        pe.parse(["--talk-tokens", "1,1", "--judge-tokens", "1,1"])
    assert "--talk-tokens and --act-tokens" in capsys.readouterr().err


def test_the_judge_follows_the_server_blocks_and_their_conditions(server, capsys):
    c, _, _ = server
    blocks = {b["id"]: b for b in c.get("/api/judge/blocks").json()}
    ledger = c.get("/api/judge/ledger").json()
    judge = run(c, capsys)["judge (llm)"]
    # DECISIONS-16: the main plan is C1, C2a, C3-accept; main 3 x 20 parents x 3, talk_prices 3 x 10 dev x 1, fixed blocks
    assert judge["calls_d13"] == 180 + 30 + 10 + 10 + blocks["wording"]["cap_per_backend"] == 250
    assert judge["calls"] == judge["calls_d13"] + ledger["retry_reserve"] == 260 <= ledger["cap_per_backend"] == 280
    assert judge["server_calls_max"] == 280 and judge["over_cap"] == []
    assert (judge["input_tokens"], judge["output_tokens"]) == (260 * 8000, 260 * 400)
    out = main(c, capsys).splitlines()
    assert any(x.startswith("judge (llm) | - | - | - | 250 | - | - | 260 |") for x in out)
    silent = run(c, capsys, "--conditions", "C1,C2a,C3-accept,C3-silent")["judge (llm)"]
    assert silent["calls_d13"] == 270  # the extension: support_one_wording judges C3-silent 20 x 1


def test_a_block_whose_plan_passes_its_server_cap_is_named(server, capsys):
    c, _, _ = server

    def api(method, path, body):
        r = c.request(method, "/api" + path, json=body)
        got = r.json()
        if path == "/judge/blocks":  # a server whose main block is too small for the plan
            got = [b | {"cap_per_backend": 100} if b["id"] == "main" else b for b in got]
        return r.status_code, got

    assert pe.main([*FAKE, "--json"], api=api) == 0
    judge = {r["condition_id"]: r for r in json.loads(capsys.readouterr().out)}["judge (llm)"]
    assert judge["over_cap"] == ["judge block main: 180 planned calls, server cap 100 per backend"]
    assert pe.main(FAKE, api=api) == 0
    assert "judge block main: 180 planned calls, server cap 100 per backend" in capsys.readouterr().out.splitlines()


def test_the_registered_plan_fits_the_server_judge_caps(server, capsys):
    """DECISIONS-13 §7.3: the plan read from conditions/ (required conditions, the seed map's dev and test seeds) fits every
    judge block cap and the cap per backend, so a plan change cannot leave the caps stale."""
    c, _, _ = server
    required = sorted(
        d["id"] for f in (ROOT / "conditions").glob("C*.yaml") if (d := yaml.safe_load(f.read_text(encoding="utf-8")))["tag"] == "required"
    )
    seeds = yaml.safe_load((ROOT / "conditions" / "seeds.yaml").read_text(encoding="utf-8"))["seeds"]
    per_split = min(sum(s["split"] == sp for s in seeds) for sp in ("dev", "test"))
    rows = run(c, capsys, "--conditions", ",".join(required), "--seeds-per-split", str(per_split))
    judge, ledger = rows["judge (llm)"], c.get("/api/judge/ledger").json()
    assert judge["over_cap"] == [], judge["over_cap"]
    assert judge["calls"] <= ledger["cap_per_backend"]


def test_model_checks_one_per_seller_model_and_paid_split(server, capsys):
    c, _, _ = server
    rows = run(c, capsys)
    checks = rows["model checks (2)"]  # one seller model (C2b's own B model is not in the plan) x dev and test
    assert checks["calls_d13"] == 2 * 2 * 4  # 2 C1 rounds, 4 calls per C1 round
    assert (checks["talk_calls"], checks["act_calls"]) == (9, 9) and checks["calls"] == 18  # 8 each x 1.1
    assert checks["input_tokens"] == round(17.6 * 3000)
    two = run(c, capsys, "--conditions", "C1,C2b")
    assert "model checks (4)" in two  # C2b's seller B is another model: its own check before each split


def test_the_judge_has_its_own_price(server, capsys):
    c, _, _ = server
    rows = run(c, capsys, "--judge-price", "10,40")
    judge, total = rows["judge (llm)"], rows["total"]
    assert judge["usd"] == round((judge["input_tokens"] * 10 + judge["output_tokens"] * 40) / 1e6, 2)
    assert total["usd"] == round(sum(r["usd"] for k, r in rows.items() if k != "total"), 2)


def test_judge_tokens_are_required(capsys):
    with pytest.raises(SystemExit):
        pe.parse(["--tokens-per-call", "1,1"])
    assert "--judge-tokens" in capsys.readouterr().err


def test_the_judge_tokens_follow_each_block_view(server, capsys):
    """CR-64 finding 4: talk_prices calls carry the price table, so each view has its own tokens per call; wording development
    (both views) and the retry reserve count at the larger pair."""
    c, _, _ = server
    pair = ("--talk-only-tokens", "5000,100", "--talk-prices-tokens", "9000,300")
    judge = run(c, capsys, *pair)["judge (llm)"]
    # talk_only: main 180 + stability 10 + robustness 10; talk_prices 30 + wording 20 + reserve 10 (no C3-silent: DECISIONS-16)
    assert (judge["input_tokens"], judge["output_tokens"]) == (200 * 5000 + 60 * 9000, 200 * 100 + 60 * 300)
    assert "Judge per call: talk_only 5000 in, 100 out; talk_prices 9000 in, 300 out" in main(c, capsys, *pair)


def test_one_judge_view_alone_is_refused(capsys):
    with pytest.raises(SystemExit):
        pe.parse(["--tokens-per-call", "1,1", "--talk-only-tokens", "1,1"])
    assert "--talk-only-tokens and --talk-prices-tokens" in capsys.readouterr().err
