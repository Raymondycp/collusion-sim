"""The installed command, run as an agent runs it, against a real server that `collusion-sim serve` starts (CLI-DESIGN §7.2)."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time

import httpx2
import pytest
from conftest import ROOT, scripted_config


def resolve_cli() -> list[str]:
    exe = shutil.which("collusion-sim", path=os.path.dirname(sys.executable))
    return [exe] if exe else [sys.executable, "-m", "simcli"]


CLI = resolve_cli()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in ("SIM_ALLOW_PAID", "SIM_API_URL", "SIM_ROOT", "SIM_PORT")}


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """One real server for the module (memory is low), stopped at the end."""
    root = tmp_path_factory.mktemp("liveroot")
    shutil.copytree(ROOT / "scenarios", root / "scenarios")
    port, log = free_port(), (root / "serve.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        CLI + ["serve", "--port", str(port), "--root", str(root), "--no-durable"],
        env=env(),
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    url = f"http://127.0.0.1:{port}/api"
    try:
        for _ in range(150):
            try:
                if httpx2.get(url + "/health", timeout=1).status_code == 200:
                    break
            except httpx2.TransportError:
                time.sleep(0.2)
        else:
            pytest.fail("server did not start")
        yield url, root, proc
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        log.close()


def run(url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(CLI + ["--server", url, *args], capture_output=True, text=True, encoding="utf-8", env=env(), timeout=120)


def test_help_lists_the_groups():
    r = subprocess.run(CLI + ["--help"], capture_output=True, text=True, env=env(), timeout=60)
    assert r.returncode == 0 and "runs" in r.stdout and "scenarios" in r.stdout


def test_serve_reports_paid_calls_off_and_health_answers(live):
    url, root, _ = live
    r = run(url, "--json", "health")
    assert r.returncode == 0 and json.loads(r.stdout)["durable_backend"] == "none"
    assert "paid calls off" in (root / "serve.log").read_text(encoding="utf-8")


def test_a_scripted_run_from_start_to_a_validated_export(live, tmp_path):
    url, root, _ = live
    config = tmp_path / "c.json"
    config.write_text(json.dumps(scripted_config()), encoding="utf-8")
    started = run(url, "--json", "runs", "start", "--config", str(config), "--wait", "--max-wait", "60")
    assert started.returncode == 0, started.stderr
    (summary,) = json.loads(started.stdout)["runs"]
    assert summary["status"] == "completed"
    out = tmp_path / "ep.jsonl"
    exported = run(url, "--json", "runs", "export", summary["id"], "--format", "jsonl", "-o", str(out))
    assert exported.returncode == 0 and json.loads(exported.stdout)["bytes"] == out.stat().st_size
    valid = subprocess.run(CLI + ["--json", "episode", "validate", str(out)], capture_output=True, text=True, env=env(), timeout=60)
    assert valid.returncode == 0 and json.loads(valid.stdout)["run_id"] == summary["id"]
    assert (root / "runs").exists() and not (tmp_path / "data").exists()


def test_a_stopped_server_gives_exit_3(live):
    port = free_port()
    r = run(f"http://127.0.0.1:{port}/api", "--json", "health")
    assert r.returncode == 3 and json.loads(r.stdout)["code"] == "server_unreachable"


@pytest.mark.parametrize("json_flag", [["--json"], []])
def test_output_is_utf8_on_a_gbk_console(live, json_flag):
    """Registry text has U+2212, which gbk cannot encode; the CLI writes UTF-8 whatever the console code page is."""
    url, _, _ = live
    gbk = {**env(), "PYTHONIOENCODING": "gbk"}
    r = subprocess.run(CLI + ["--server", url, *json_flag, "policies", "list"], capture_output=True, env=gbk, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = r.stdout.decode("utf-8")
    assert "−" in out
    if json_flag:
        assert json.loads(out)
