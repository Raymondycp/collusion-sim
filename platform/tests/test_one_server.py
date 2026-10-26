"""One server per data folder, and a held run shows why (de/one-server; SE-case-presets-report addendum 2)."""

import os
import shutil
import signal
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from adapters.server_lock import DataFolderInUse, ServerLock
from api.app import create_app
from services.configs import ApiError
from simcli.main import cli
from test_api import check, config
from test_research import ROOT

HOLDER = """
import os, sys, time
sys.path.insert(0, sys.argv[2])
from adapters.server_lock import ServerLock
ServerLock(sys.argv[1]).acquire()
print(os.getpid(), flush=True)
time.sleep(60)
"""


def app_root(tmp_path):
    shutil.copytree(ROOT / "scenarios", tmp_path / "scenarios")
    return tmp_path


def test_a_second_app_on_the_same_data_folder_refuses_to_start_until_the_first_stops(tmp_path):
    root = app_root(tmp_path)
    with TestClient(create_app(root, use_dbos=False)):
        with pytest.raises(DataFolderInUse) as refused:
            with TestClient(create_app(root, use_dbos=False)):
                pass
        assert str(root / "data") in str(refused.value) and "pid" in str(refused.value)
    with TestClient(create_app(root, use_dbos=False)) as again:
        assert again.get("/api/health").status_code == 200, "the lock is released at shutdown"


def test_another_process_holds_the_lock_until_it_ends(tmp_path):
    data = tmp_path / "data"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(data), str(ROOT / "src")], stdout=subprocess.PIPE, text=True)
    try:
        pid = int(holder.stdout.readline())  # the interpreter's pid; on Windows the venv python.exe is a launcher
        with pytest.raises(DataFolderInUse) as refused:
            ServerLock(data).acquire()
        assert f"pid {pid}" in str(refused.value)
        os.kill(pid, signal.SIGTERM)
    finally:
        holder.kill()
        holder.wait(timeout=10)
    lock = ServerLock(data)
    lock.acquire()  # a killed holder leaves no stale lock
    lock.release()


def test_serve_refuses_a_data_folder_in_use_and_starts_nothing(tmp_path, monkeypatch):
    for name in ("SIM_ROOT", "SIM_DURABLE", "SIM_PORT"):  # serve sets them; monkeypatch puts them back
        monkeypatch.delenv(name, raising=False)
    started = []
    monkeypatch.setattr("runpy.run_module", lambda *a, **k: started.append(a))
    lock = ServerLock(tmp_path / "data")
    lock.acquire()
    try:
        out = CliRunner().invoke(cli, ["--json", "serve", "--root", str(tmp_path), "--no-durable"])
    finally:
        lock.release()
    assert out.exit_code == 1 and '"code": "data_folder_in_use"' in out.output and not started
    assert CliRunner().invoke(cli, ["serve", "--root", str(tmp_path), "--no-durable"]).exit_code == 0 and started


def test_serve_flags_decide_dbos_and_read_the_paid_switch_the_gate_reads(tmp_path, monkeypatch):
    """A review addendum: `--no-durable` must beat an exported SIM_DURABLE=1, and the banner must use the same reader
    as the paid gate (an empty process variable falls back to .env)."""
    monkeypatch.setenv("SIM_DURABLE", "1")
    monkeypatch.delenv("SIM_PORT", raising=False)
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    (tmp_path / ".env").write_text("SIM_ALLOW_PAID=1\n", encoding="utf-8")
    monkeypatch.setattr("runpy.run_module", lambda *a, **k: None)
    out = CliRunner().invoke(cli, ["serve", "--root", str(tmp_path)])
    assert out.exit_code == 0 and os.environ["SIM_DURABLE"] == "1", "an exported switch stays when no flag is given"
    assert "paid calls on" in out.stderr
    assert CliRunner().invoke(cli, ["serve", "--root", str(tmp_path), "--no-durable"]).exit_code == 0
    assert os.environ["SIM_DURABLE"] == "0", "--no-durable beats the environment"
    assert CliRunner().invoke(cli, ["serve", "--root", str(tmp_path), "--durable"]).exit_code == 0
    assert os.environ["SIM_DURABLE"] == "1"


def test_a_run_this_server_cannot_build_is_recovering_with_its_reason_before_any_event(tmp_path, monkeypatch, caplog):
    root = app_root(tmp_path)
    with TestClient(create_app(root, use_dbos=False)) as c:
        svc = c.app.state.svc
        build = svc.builder

        def refuse(run_id):
            raise ApiError(409, "paid_calls_disabled", "Paid calls are off on this server.")

        svc.builder = refuse
        (rid,) = check(c.post("/api/runs", json=config()), "/runs", "post")["run_ids"]
        deadline = time.monotonic() + 30
        while rid not in c.app.state.db.held() and time.monotonic() < deadline:
            time.sleep(0.05)
        run = check(c.get(f"/api/runs/{rid}"), "/runs/{run_id}")
        assert (run["status"], run["hold_reason"]) == ("recovering", "paid_calls_disabled")
        assert c.get(f"/api/runs/{rid}/events").json()["events"] == [], "no event before run_started"
        listed = check(c.get("/api/runs", params={"status": "recovering"}), "/runs")["items"]
        assert [r["id"] for r in listed] == [rid] and listed[0]["hold_reason"] == "paid_calls_disabled"
        assert f"run {rid} cannot resume on this server (paid_calls_disabled)" in caplog.text
        svc.builder = build
        svc.launch(rid)
        svc.wait(rid, timeout=30)
        run = check(c.get(f"/api/runs/{rid}"), "/runs/{run_id}")
        assert (run["status"], run["hold_reason"]) == ("completed", None), "a run that starts loses its hold"
        assert rid not in c.app.state.db.held()
