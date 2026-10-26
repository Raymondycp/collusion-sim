"""Item 3 unit J3: the LLM judge through the existing Endpoint, on the fake OpenAI-compatible endpoint. No paid call."""

import json
import shutil
import sqlite3
import threading

import pytest
import yaml
from fastapi.testclient import TestClient

from adapters.llm.endpoint import Endpoint
from adapters.store import Writer
from api.app import create_app
from fakellm import FakeLLM, text
from judge.store import EvalDB
from services.configs import ApiError
from services.evaluations import EvaluationCreate, EvaluationService
from services.runner import new_id
from test_api import check as contract
from test_judge import MODELS, RUBRIC_TEXT, _talkers, write_models
from test_profiles import MemoryKeyring, no_key_anywhere, profile_body
from test_research import ROOT, freeze_condition, research_config, started
from condcopy import draft_conditions

OK = json.dumps({"quotes": [], "commitments": [], "rationale": "No offer is made.", "agreement": 0, "talk_score": 1})


class Brain:
    """Replies in order from `script`, then `then` for every later call."""

    def __init__(self) -> None:
        self.script: list = []
        self.then = text(OK)

    def __call__(self, body: dict):
        return self.script.pop(0) if self.script else self.then


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    draft_conditions(tmp_path / "conditions")
    (tmp_path / "conditions" / "judge-rubric.yaml").write_text(RUBRIC_TEXT, encoding="utf-8")
    write_models(tmp_path / "conditions")
    return tmp_path


@pytest.fixture
def client(root):
    brain = Brain()
    fake = FakeLLM(brain)
    with TestClient(create_app(root, use_dbos=False, key_backend=MemoryKeyring(), llm_transport=fake.transport)) as c:
        c.fake, c.brain = fake, brain
        c.pid = c.post("/api/provider-profiles", json=profile_body()).json()["id"]
        judge = {"provider_profile_id": c.pid, "model": "m-small", "model_family": "judge-fam"}  # the judge of body()
        models = yaml.safe_dump({**MODELS, "judge": judge}, sort_keys=False)
        assert c.put("/api/research/models", json={"text": models}).status_code == 200  # fills base_url and protocol
        yield c


def own_service(client, root) -> EvaluationService:
    """A second service on the same files with no worker: the test runs the job itself."""
    s = client.app.state
    eval_db = EvalDB(root / "data" / "eval.sqlite")
    svc = EvaluationService(s.db, eval_db, Writer(eval_db), s.research, s.judge, new_id, s.evaluations.llm)
    svc.close()
    return svc


def body(client, run_ids, **kw) -> EvaluationCreate:
    llm = {"provider_profile_id": client.pid, "model": "m-small", "model_family": "judge-fam"}
    raw = {"run_ids": run_ids, "block": "wording", "view": "talk_only", "item_set_id": "items-test", "backends": {"llm": llm}}
    return EvaluationCreate.model_validate(raw | kw)


def pilot_runs(client, seeds=(1,)) -> list[str]:
    return started(client, research_config("C1", seeds=seeds, policies=_talkers("C1")))


def judged(client, run_ids) -> dict:
    svc = client.app.state.evaluations
    return svc.wait(svc.create(body(client, run_ids))["id"])


def statuses(job: dict, paraphrase: int) -> list[str]:
    return [c["status"] for c in job["episodes"][0]["calls"] if c["paraphrase"] == paraphrase]


def test_each_llm_call_writes_a_charged_sent_row_before_its_result_and_sends_the_transcript_unchanged(client, root):
    [rid] = pilot_runs(client)
    job = judged(client, [rid])
    assert job["status"] == "completed" and job["episodes"][0]["llm"]["n_ok"] == 3
    assert all(statuses(job, k) == ["sent", "ok"] for k in (1, 2, 3))
    assert [c["charged"] for c in job["episodes"][0]["calls"]] == [True, False] * 3
    ledger = client.app.state.evaluations.ledger()["backends"]["llm"]
    assert (ledger["used"], ledger["retries_used"], ledger["held"]) == (3, 0, 0)
    rendered = client.app.state.judge.rendered(rid, "talk_only", "none")
    for sent in client.fake.bodies:
        assert sent["temperature"] == 0 and rendered.text in sent["messages"][-1]["content"]
        assert rid not in json.dumps(sent)
    assert all(h.get("authorization", "").startswith("Bearer ") for h in client.fake.headers)
    no_key_anywhere(root, json.dumps(job), json.dumps(client.app.state.evaluations.ledger()))


def test_an_invalid_output_is_retried_once_from_the_reserve(client):
    [rid] = pilot_runs(client)
    client.brain.script = [text("not json"), text(OK)]
    job = judged(client, [rid])
    tries = [k for k in (1, 2, 3) if statuses(job, k) == ["sent", "invalid", "sent", "ok"]]
    assert len(tries) == 1 and job["episodes"][0]["llm"]["n_ok"] == 3
    retry = next(c for c in job["episodes"][0]["calls"] if c["retry_of"] is not None)
    assert retry["status"] == "sent" and retry["charged"] is True
    ledger = client.app.state.evaluations.ledger()["backends"]["llm"]
    assert (ledger["used"], ledger["retries_used"]) == (3, 1)


def test_two_invalid_outputs_leave_the_paraphrase_invalid(client):
    [rid] = pilot_runs(client)
    client.brain.script = [text("{}"), text("no")]
    job = judged(client, [rid])
    assert sorted(job["per_backend"]["llm"]["counts"].items()) == [("invalid", 2), ("ok", 2), ("sent", 4)]
    assert job["episodes"][0]["llm"]["n_ok"] == 2


def test_a_reply_with_no_text_is_refused_and_a_server_error_is_recorded(client):
    [rid] = pilot_runs(client)
    client.brain.script = [text(""), 500]
    job = judged(client, [rid])
    counts = job["per_backend"]["llm"]["counts"]
    assert (counts["refused"], counts["error"], counts["ok"], job["status"]) == (1, 1, 1, "completed")
    error = next(c for c in job["episodes"][0]["calls"] if c["status"] == "error")
    assert error["error_reason"] == "provider_error"


def test_a_bad_key_stops_the_job_as_failed_and_it_can_resume(client):
    [rid] = pilot_runs(client)
    client.brain.script = [401]
    job = judged(client, [rid])
    assert (job["status"], job["error"]["code"]) == ("failed", "conflict") and "auth" in job["error"]["message"]
    again = contract(client.post(f"/api/evaluations/{job['id']}/resume"), "/evaluations/{evaluation_id}/resume", "post")
    done = client.app.state.evaluations.wait(again["id"])
    assert done["status"] == "completed" and done["episodes"][0]["llm"]["n_ok"] == 3  # the call that met 401 is made again
    assert len(client.fake.bodies) == 4 and client.app.state.evaluations.ledger()["backends"]["llm"]["used"] == 4


def test_a_second_job_on_the_same_input_is_served_from_the_cache_and_charges_nothing(client):
    [rid] = pilot_runs(client)
    judged(client, [rid])
    sent = len(client.fake.bodies)
    job = judged(client, [rid])
    assert len(client.fake.bodies) == sent and job["per_backend"]["llm"]["charged"] == 0
    assert all(c["cache_hit"] for c in job["episodes"][0]["calls"]) and job["episodes"][0]["llm"]["n_ok"] == 3
    assert client.app.state.evaluations.ledger()["backends"]["llm"]["used"] == 3


class Died(BaseException):
    """The server process ends while a request is on the wire."""


def test_a_call_sent_before_a_crash_counts_as_spent_and_is_not_sent_again(client, root, monkeypatch):
    [rid] = pilot_runs(client)
    svc = own_service(client, root)
    eid = svc.create(body(client, [rid]))["id"]
    real, count = Endpoint.request, {"n": 0}

    def die_on_the_second(self, req):
        count["n"] += 1
        if count["n"] == 2:
            raise Died
        return real(self, req)

    monkeypatch.setattr(Endpoint, "request", die_on_the_second)
    with pytest.raises(Died):
        svc._run(eid)
    monkeypatch.undo()
    again = own_service(client, root)
    again._run_safely(eid)
    done = again.detail(eid)
    assert done["status"] == "completed" and len(client.fake.bodies) == 2  # 3 planned; the one on the wire is spent
    assert sorted(len(statuses(done, k)) for k in (1, 2, 3)) == [1, 2, 2]
    assert again.ledger()["backends"]["llm"]["used"] == 3


def test_recover_starts_no_paid_job_while_paid_calls_are_off(client, root, monkeypatch):
    [rid] = pilot_runs(client)
    svc = own_service(client, root)
    eid = svc.create(body(client, [rid]))["id"]
    svc._queue.queue.clear()  # create() queued it; recover() must not
    monkeypatch.setattr(svc.llm, "transport", None)
    svc.recover()
    assert eid not in svc._queue.queue and svc.detail(eid)["status"] == "queued" and client.fake.bodies == []


def test_a_failed_job_without_test_cells_frees_its_room_and_its_resume_checks_the_cap(client, root):
    rids = pilot_runs(client, seeds=(1, 2, 3))
    svc = own_service(client, root)
    first = svc.create(body(client, rids))["id"]  # 9 of the wording cap of 20
    rubric = root / "conditions" / "judge-rubric.yaml"
    rubric.write_text(RUBRIC_TEXT + "\n# edited\n", encoding="utf-8")
    svc._run_safely(first)
    assert svc.detail(first)["status"] == "failed" and svc.ledger()["backends"]["llm"]["held"] == 0
    rubric.write_text(RUBRIC_TEXT, encoding="utf-8")
    svc.create(body(client, rids))
    svc.create(body(client, rids))  # 18 held
    with pytest.raises(ApiError) as e:
        svc.resume(first)
    assert (e.value.status, e.value.code) == (409, "evaluation_budget_exceeded")
    assert svc.detail(first)["status"] == "failed"


def test_two_parallel_jobs_that_each_fit_alone_store_one(client, root, monkeypatch):
    rids = pilot_runs(client, seeds=(1, 2, 3))
    svc = own_service(client, root)
    svc.create(body(client, rids[:1]))  # 3 held of the wording cap of 20; each next job adds 9
    real, barrier = svc.plan, threading.Barrier(2)

    def plan(b, eid):
        out = real(b, eid)
        barrier.wait(timeout=10)  # both read the same room before either stores
        return out

    monkeypatch.setattr(svc, "plan", plan)
    results = []

    def go():
        try:
            results.append(svc.create(body(client, rids))["id"])
        except ApiError as e:
            results.append(e.code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r == "evaluation_budget_exceeded" for r in results) == [False, True]
    assert len(svc.eval_db.evaluations()) == 2 and svc.ledger()["backends"]["llm"]["held"] == 12


def test_the_llm_profile_is_checked_at_plan_time(client):
    [rid] = pilot_runs(client)
    svc = client.app.state.evaluations
    with pytest.raises(ApiError) as e:
        svc.estimate(body(client, [rid], backends={"llm": {"provider_profile_id": "prof_nope", "model": "m", "model_family": "f"}}))
    assert e.value.code == "not_found"
    est = contract(
        client.post("/api/evaluations/estimate", json=body(client, [rid]).model_dump(exclude_none=True)), "/evaluations/estimate", "post"
    )
    assert est["per_backend"]["llm"]["calls"] == 3 and est["per_backend"]["llm"]["within_cap"] is True


# CR-36 fixes


def test_two_errors_of_one_kind_in_a_row_stop_the_job_and_the_resume_makes_those_calls_again(client):
    [rid] = pilot_runs(client)
    client.brain.script = [500, 500]
    job = judged(client, [rid])
    assert (job["status"], job["error"]["code"]) == ("failed", "conflict") and "2 judge calls in a row failed with provider_error" in (
        job["error"]["message"]
    )
    assert len(client.fake.bodies) == 2 and client.app.state.evaluations.ledger()["backends"]["llm"]["used"] == 2
    svc = client.app.state.evaluations
    done = svc.wait(svc.resume(job["id"])["id"])
    assert done["status"] == "completed" and done["episodes"][0]["llm"]["n_ok"] == 3 and len(client.fake.bodies) == 5


def test_the_stability_block_calls_again_instead_of_reading_the_cache(client, root):
    freeze_condition(client, root, "C1")
    [rid] = started(client, research_config("C1", split="dev", seeds=(101,), policies=_talkers("C1")))
    svc = client.app.state.evaluations
    jobs = [svc.wait(svc.create(body(client, [rid], block="stability"))["id"]) for _ in range(2)]
    assert len(client.fake.bodies) == 2 and [j["per_backend"]["llm"]["charged"] for j in jobs] == [1, 1]
    assert not any(c["cache_hit"] for j in jobs for c in j["episodes"][0]["calls"])


def test_a_chain_broken_during_a_job_stops_the_next_paid_request(client, root):
    [rid] = pilot_runs(client)
    path = client.app.state.evaluations.eval_db.path

    def break_the_chain_on_the_first_call(body):
        if len(client.fake.bodies) == 1:
            conn = sqlite3.connect(path)
            conn.execute("DROP TRIGGER judge_calls_no_update")
            conn.execute("UPDATE judge_calls SET entry_json = replace(entry_json, 'sent', 'ok') WHERE line = 1")
            conn.commit()
            conn.close()
        return text(OK)

    client.fake.brain = break_the_chain_on_the_first_call
    job = judged(client, [rid])
    assert (job["status"], job["error"]["code"]) == ("failed", "conflict") and "hash chain is broken at line 1" in job["error"]["message"]
    assert len(client.fake.bodies) == 1


def test_the_session_header_carries_the_evaluation_and_view_and_stays_out_of_the_cache_key(client):
    [rid] = pilot_runs(client)
    created = client.post("/api/provider-profiles", json=profile_body(name="With session", session_header="x-opencode-session"))
    client.pid = created.json()["id"]
    first = judged(client, [rid])
    assert [h["x-opencode-session"] for h in client.fake.headers] == [f"{first['id']}:talk_only"] * 3
    second = judged(client, [rid])
    assert second["id"] != first["id"] and len(client.fake.bodies) == 3 and second["per_backend"]["llm"]["charged"] == 0
