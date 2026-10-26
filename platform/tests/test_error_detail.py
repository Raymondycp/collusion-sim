# ruff: noqa: F811 (the fixtures client, llm_client and ring come from test_api and test_profiles)
"""run_ended.error_detail (UX-omp-review-2 item 2; DE-d22-report.md "Next" §2, approved by the lead 23 Oct): a failed run
records the most specific cause the server has, and GET /runs/{id} returns it. Written before the code (red until the unit
is built after C1c)."""

import re

import httpx2
import pytest

from fakellm import cooperator, tools_offered
from simcore.recorder import LATER_FIELDS, _hash_line
from simschema.events import ErrorDetail, RunEndedEvent
from test_api import check, client, config  # noqa: F401
from test_profiles import llm_client, llm_config, profile_body, ring  # noqa: F401

KEYISH = "sk-live-0123456789abcdefXYZ"


def round_of(body: dict) -> int:
    user = next(m["content"] for m in body["messages"] if m["role"] == "user")
    return int(re.search(r"Round (\d+)\.", user).group(1))


def ended(c, rid: str) -> dict:
    (event,) = check(c.get(f"/api/runs/{rid}/events", params={"types": "run_ended"}), "/runs/{run_id}/events")["events"]
    return event


def test_a_fatal_401_in_an_act_call_records_where_and_the_providers_status_and_message(llm_client):
    c = llm_client
    reply = cooperator(10.5)
    c.fake.brain = lambda body: 401 if round_of(body) == 2 and "set_price" in tools_offered(body) else reply(body)
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid)).json()["run_ids"][0]
    assert c.app.state.svc.wait(rid, timeout=60)["status"] == "failed"
    detail = ended(c, rid)["error_detail"]
    assert detail["code"] == "auth" and detail["where"] == {"phase": "act", "seller_id": "A", "t": 2}
    assert (detail["http_status"], detail["provider_code"], detail["provider_message"]) == (401, "server_error", "fake failure")
    assert detail["exception"] == "EndpointError"
    run = check(c.get(f"/api/runs/{rid}"), "/runs/{run_id}")
    assert (run["end_reason"], run["error"], run["error_detail"]) == ("error", "auth", detail), "the run reads its run_ended"


def test_a_410_keeps_the_long_model_id_in_the_provider_message(llm_client):
    """CR-102: the provider's text is masked once, with the key, so a model id of 16 or more characters stays readable."""
    c = llm_client
    text = "Model deepseek-v4.1-flash-free was removed; try claude-sonnet-5-5"
    gone = httpx2.Response(410, json={"error": {"message": text, "code": "model_removed"}})
    c.fake.brain = lambda body: gone
    pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
    rid = c.post("/api/runs", json=llm_config(pid)).json()["run_ids"][0]
    assert c.app.state.svc.wait(rid, timeout=60)["status"] == "failed"
    detail = ended(c, rid)["error_detail"]
    assert (detail["http_status"], detail["provider_code"], detail["provider_message"]) == (410, "model_removed", text)


def test_an_exception_in_a_seller_turn_records_its_class_and_a_masked_message(client, monkeypatch):
    from simcore.scripted import GrimTrigger

    act = GrimTrigger.act

    def broken(self, obs, tools):
        if obs.t == 3:
            raise RuntimeError(f"boom with {KEYISH} in it for deepseek-v4.1-flash-free")
        return act(self, obs, tools)

    monkeypatch.setattr(GrimTrigger, "act", broken)
    (rid,) = check(client.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
    assert client.app.state.svc.wait(rid, timeout=60)["status"] == "failed"
    event = ended(client, rid)
    detail = event["error_detail"]
    assert (event["error"], detail["code"], detail["exception"]) == ("RuntimeError", "RuntimeError", "RuntimeError")
    assert detail["where"] == {"phase": "act", "seller_id": "B", "t": 3}
    assert detail["message"] == "boom with [redacted] in it for deepseek-v4.1-flash-free" and detail["http_status"] is None
    assert KEYISH not in str(check(client.get(f"/api/runs/{rid}"), "/runs/{run_id}"))


def test_a_failure_outside_a_seller_call_has_no_where(client, monkeypatch):
    from simcore.market import Market

    settle = Market.settle

    def broken(self, prices, p):
        broken.calls += 1
        if broken.calls == 3:
            raise KeyError("missing")
        return settle(self, prices, p)

    broken.calls = 0
    monkeypatch.setattr(Market, "settle", broken)
    (rid,) = check(client.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
    assert client.app.state.svc.wait(rid, timeout=60)["status"] == "failed"
    detail = ended(client, rid)["error_detail"]
    assert (detail["code"], detail["exception"], detail["where"]) == ("KeyError", "KeyError", None)


@pytest.mark.parametrize("error", [None, "auth"])
def test_a_run_ended_line_without_error_detail_hashes_as_before_the_field(error):
    """error_detail is a LATER_FIELDS field: null is left out, so an episode stored before the field re-hashes to its hash."""
    assert "error_detail" in LATER_FIELDS["run_ended"]
    e = RunEndedEvent(seq=9, run_id="run_X", t=3.0, wall_time="2026-10-23T00:00:00.000Z", tier="B", status="failed",
                      end_reason="error", episode_hash="sha256:" + "0" * 64, error=error)  # fmt: skip
    assert _hash_line(e) == e.model_dump_json(exclude={"wall_time", "error_detail"}) + "\n"
    detail = ErrorDetail(code="auth", exception="EndpointError")
    assert '"error_detail":{"code":"auth"' in _hash_line(e.model_copy(update={"error_detail": detail})), "a set one is hashed"
