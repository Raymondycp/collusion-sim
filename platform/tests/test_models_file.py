# ruff: noqa: F811 (the fixtures root and client come from test_llm_judge)
"""conditions/models.yaml (CR-43 M1, CR-47): a stage-conditions freeze input; dev and test starts, child starts and judge
jobs use its models and the sellers' sampling settings."""

import shutil
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.app import create_app
from fakellm import FakeLLM, cooperator
from services import models_file
from services.configs import ApiError
from test_api import check
from test_judge import MODELS, RUBRIC_TEXT, _talkers
from test_llm_judge import body, client, root  # noqa: F401 (fixtures)
from test_profiles import MemoryKeyring, profile_body
from test_research import ROOT, freeze_condition, research_config, started
from condcopy import draft_conditions

SAMPLING = {"temperature": 0.7, "top_p": None, "max_output_tokens": None, "timeout_s": 60, "thinking": None, "thinking_on_timeout": None}


def seller(client, model="m-small", family="seller-fam", **kw) -> dict:
    return {"provider_profile_id": client.pid, "model": model, "model_family": family, **kw}


def entry(client, **kw) -> dict:
    return seller(client, **kw) | SAMPLING


def put_models(client, **kw):
    judge = {"provider_profile_id": client.pid, "model": "m-small", "model_family": "judge-fam"}
    return client.put("/api/research/models", json={"text": yaml.safe_dump({**MODELS, "judge": judge, **kw}, sort_keys=False)})


def put_llm_models(client):
    """Both sellers on one LLM model; C2b gives B another family, as its condition needs."""
    both = {"A": entry(client), "B": entry(client)}
    return put_models(client, sellers=both, by_condition={"C2b": {"B": entry(client, family="seller-fam-2")}})


def llm_config(client, split="dev", seeds=(101,), **llm) -> dict:
    cfg = research_config("C1", split=split, seeds=seeds)
    for a in cfg["agents"]:
        a["policy"] = {"kind": "llm", "llm": seller(client, **llm)}
    return cfg


def freeze_errors(client) -> tuple[str, list[str]]:
    r = client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"}).json()
    return r.get("code"), [e["path"] for e in r.get("errors", [])]


# PUT /research/models


def test_put_fills_base_url_and_protocol_of_each_named_profile(client, root):
    assert put_models(client, sellers={"A": entry(client), "B": "scripted"}).status_code == 200
    saved = yaml.safe_load((root / "conditions" / "models.yaml").read_text(encoding="utf-8"))
    profile = client.get(f"/api/provider-profiles/{client.pid}").json()
    for e in (saved["judge"], saved["sellers"]["A"]):
        assert (e["base_url"], e["protocol"]) == (profile["base_url"], profile["protocol"])
    assert saved["sellers"]["B"] == "scripted"


def test_a_seller_entry_without_thinking_on_timeout_is_valid_and_keeps_thinking():
    """CR-91 minor 1: a file written before DECISIONS-22 entry 5.3 still parses; the other settings stay required."""
    old = {"provider_profile_id": "p", "model": "m", "model_family": "f"} | {
        k: v for k, v in SAMPLING.items() if k != "thinking_on_timeout"
    }
    assert models_file.SellerEntry.model_validate(old).thinking_on_timeout is None
    with pytest.raises(ValidationError, match="thinking"):
        models_file.SellerEntry.model_validate({k: v for k, v in old.items() if k != "thinking"})


def test_put_refuses_an_unknown_profile_with_its_path(client, root):
    before = (root / "conditions" / "models.yaml").read_bytes()
    r = check(put_models(client, sellers={"A": entry(client) | {"provider_profile_id": "prof_nope"}}), "/research/{name}", "put")
    assert (r["status"], r["code"]) == (422, "validation_error") and [e["path"] for e in r["errors"]] == ["sellers/A/provider_profile_id"]
    assert (root / "conditions" / "models.yaml").read_bytes() == before


@pytest.mark.parametrize("url", ["https://user:pw@llm.example.test/v1", "https://llm.example.test/v1?api_key=x"])
def test_a_base_url_with_credentials_or_a_query_is_refused_by_the_profile_and_by_the_fill(client, url):
    r = client.post("/api/provider-profiles", json=profile_body(base_url=url))
    assert r.status_code == 422 and "user name" in r.text

    def record(_id):  # a record that bypassed the profile check
        return SimpleNamespace(base_url=url, protocol="openai_chat")

    with pytest.raises(ApiError) as e:
        models_file.fill({"judge": {"provider_profile_id": "p", "model": "m", "model_family": "f"}}, record)
    assert (e.value.status, e.value.errors[0]["path"]) == (422, "judge/provider_profile_id")


# The stage-conditions freeze


def test_stage_conditions_needs_the_models_file(client, root):
    (root / "conditions" / "models.yaml").unlink()
    r = client.post("/api/freeze", json={"stage": "conditions", "approved_by": "lead"}).json()
    assert r["code"] == "freeze_incomplete" and "models.yaml" in r["message"]


@pytest.mark.parametrize(
    ("change", "paths"),
    [
        (lambda d: d.pop("judge"), ["judge"]),
        (lambda d: d["judge"].update(model="TEAM TO WRITE"), ["judge/model"]),
        (lambda d: d["judge"].pop("base_url"), ["judge/base_url"]),
        (lambda d: d["sellers"]["A"].pop("temperature"), ["sellers/A/SellerEntry/temperature", "sellers/A/literal['scripted']"]),
        (lambda d: d["sellers"].pop("B"), ["sellers/B"] * 6),
        (lambda d: d["judge"].update(model_family="seller-fam"), ["judge/model_family"]),
        (lambda d: d["by_condition"].pop("C2b"), ["by_condition/C2b"]),
        (lambda d: d["by_condition"].update(C2a={"B": "scripted"}), ["by_condition/C2a"]),
        (lambda d: d["by_condition"].update(C2a={"B": {**d["sellers"]["A"], "model_family": "seller-fam-3"}}), ["by_condition/C2a"]),
        (lambda d: d["by_condition"].update(C2B=d["by_condition"]["C2b"]), ["by_condition/C2B"]),
        (lambda d: d["by_condition"]["C2b"].update(Z="scripted"), ["by_condition/C2b/Z"]),
        (lambda d: d["sellers"].update(Z="scripted"), ["sellers/Z"]),
    ],
    ids=[
        "no_judge", "placeholder", "no_base_url", "no_temperature", "no_seller", "judge_family_of_a_seller", "c2b_one_family",
        "c2a_two_models", "c2a_two_families", "unknown_condition", "unknown_override_seller", "unknown_seller",
    ],
)  # fmt: skip
def test_stage_conditions_refuses_a_models_file_that_cannot_be_frozen(client, root, change, paths):
    assert put_llm_models(client).status_code == 200
    path = root / "conditions" / "models.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    code, got = freeze_errors(client)
    assert (code, got) == ("freeze_incomplete", paths)
    assert not (root / "conditions" / "frozen.json").exists()


def test_stage_conditions_refuses_a_file_whose_profile_changed_after_the_put(client, root):
    assert put_llm_models(client).status_code == 200
    assert client.patch(f"/api/provider-profiles/{client.pid}", json={"base_url": "https://other.example.test/v1"}).status_code == 200
    code, got = freeze_errors(client)
    want = ["sellers/A/provider_profile_id", "sellers/B/provider_profile_id", "by_condition/C2b/B/provider_profile_id"]
    assert (code, got) == ("freeze_incomplete", [*want, "judge/provider_profile_id"])


def test_the_models_file_is_read_only_once_stage_conditions_is_frozen(client, root):
    freeze_condition(client, root, "C1")
    r = check(put_models(client), "/research/{name}", "put")
    assert (r["status"], r["code"]) == (409, "research_frozen")


# Dev and test starts


def test_a_dev_start_with_the_frozen_seller_models_passes_and_another_model_is_refused(client, root):
    assert put_llm_models(client).status_code == 200
    freeze_condition(client, root, "C1")
    check(client.post("/api/runs/estimate", json=llm_config(client)), "/runs/estimate", "post")
    for path, cfg in [("/api/runs/estimate", llm_config(client, model="m-large")), ("/api/runs", llm_config(client, model="m-large"))]:
        r = client.post(path, json=cfg).json()
        assert (r["status"], r["code"]) == (409, "model_not_frozen")
        assert [e["path"] for e in r["errors"]] == ["agents/0/policy/llm/model", "agents/1/policy/llm/model"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 0.2),
        ("top_p", 0.9),
        ("max_output_tokens", 800),
        ("timeout_s", 300),
        ("thinking", "low"),
        ("thinking_on_timeout", "minimal"),  # D22 entry 5.3
    ],  # D18
)
def test_a_dev_start_with_other_sampling_settings_is_refused(client, root, field, value):
    assert put_llm_models(client).status_code == 200
    freeze_condition(client, root, "C1")
    r = client.post("/api/runs/estimate", json=llm_config(client, **{field: value})).json()
    assert r["code"] == "model_not_frozen"
    assert [e["path"] for e in r["errors"]] == [f"agents/0/policy/llm/{field}", f"agents/1/policy/llm/{field}"]


def test_a_dev_experiment_with_another_family_is_refused(client, root):
    assert put_llm_models(client).status_code == 200
    freeze_condition(client, root, "C1")
    agents = llm_config(client, family="other-fam")["agents"]
    req = {
        "conditions": ["C1"],
        "split": "dev",
        "seeds": [101],
        "agents": agents,
        "budget": {"max_usd": 1.0},
        "batch_budget": {"max_calls": 100},
    }
    r = client.post("/api/experiments/estimate", json=req).json()
    assert (r["status"], r["code"]) == (409, "model_not_frozen") and r["errors"][0]["path"] == "agents/0/policy/llm/model_family"


def test_an_llm_seller_where_the_file_says_scripted_is_refused_and_pilot_is_not_checked(client, root):
    freeze_condition(client, root, "C1")  # the fixture's file: both sellers scripted
    r = client.post("/api/runs/estimate", json=llm_config(client)).json()
    assert (r["code"], r["errors"][0]["path"]) == ("model_not_frozen", "agents/0/policy/kind")
    check(client.post("/api/runs/estimate", json=llm_config(client, split="pilot", seeds=(1,), model="m-any")), "/runs/estimate", "post")


def test_a_profile_pointed_elsewhere_after_the_freeze_is_refused(client, root):
    assert put_llm_models(client).status_code == 200
    freeze_condition(client, root, "C1")
    assert client.patch(f"/api/provider-profiles/{client.pid}", json={"base_url": "https://other.example.test/v1"}).status_code == 200
    r = client.post("/api/runs/estimate", json=llm_config(client)).json()
    assert r["code"] == "model_not_frozen" and "base_url" in r["errors"][0]["message"]
    assert r["errors"][0]["path"] == "agents/0/policy/llm/provider_profile_id"


@pytest.fixture
def market(tmp_path, monkeypatch):
    """LLM sellers on a fake endpoint that always prices 10.5: a dev parent that completes."""
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    (tmp_path / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    fake = FakeLLM(cooperator(10.5))
    with TestClient(create_app(tmp_path, use_dbos=False, key_backend=MemoryKeyring(), llm_transport=fake.transport)) as c:
        c.pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
        c.root = tmp_path
        yield c


def test_a_child_of_a_dev_parent_is_refused_once_its_profile_points_elsewhere(market):
    assert put_llm_models(market).status_code == 200
    freeze_condition(market, market.root, "C1")
    [rid] = started(market, llm_config(market))
    assert market.get(f"/api/runs/{rid}").json()["status"] == "completed"
    branch = {"at": 3, "rounds": 2, "intervention": {"type": "placebo"}}
    assert market.patch(f"/api/provider-profiles/{market.pid}", json={"base_url": "https://other.example.test/v1"}).status_code == 200
    r = market.post(f"/api/runs/{rid}/branches", json=branch).json()
    assert (r["code"], r["errors"][0]["path"]) == ("model_not_frozen", "agents/0/policy/llm/provider_profile_id")


# Judge jobs


def test_a_dev_llm_judge_job_uses_the_frozen_judge_and_a_pilot_job_is_not_checked(client, root):
    freeze_condition(client, root, "C1")
    [dev] = started(client, research_config("C1", split="dev", seeds=(101,), policies=_talkers("C1")))
    [pilot] = started(client, research_config("C1", seeds=(1,), policies=_talkers("C1")))
    svc = client.app.state.evaluations
    other = {"provider_profile_id": client.pid, "model": "m-large", "model_family": "judge-fam"}
    with pytest.raises(ApiError) as e:
        svc.estimate(body(client, [dev], block="stability", backends={"llm": other}))
    assert (e.value.status, e.value.code) == (409, "model_not_frozen") and e.value.errors[0]["path"] == "backends/llm/model"
    svc.estimate(body(client, [dev], block="stability"))
    svc.estimate(body(client, [pilot], backends={"llm": other}))
