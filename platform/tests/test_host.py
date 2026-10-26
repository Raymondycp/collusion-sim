"""scripts/host.py (se/host): argument defaults, the paid switch, key masking, the refusal line, and the port-in-use
path, which must stop before it starts or stops any process. No server, no web, no network."""

import importlib.util
import socket
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("host", REPO / "scripts" / "host.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def test_defaults_are_the_repo_root_port_8000_web_5173_browser_on_and_paid_off():
    args = host.parse([])
    assert (args.root, args.port, args.web_port, args.no_browser, args.paid) == (REPO, 8000, 5173, False, False)


def test_options_are_parsed_and_the_root_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = host.parse(["--root", "data-copy", "--port", "8765", "--web-port", "5180", "--no-browser", "--paid"])
    assert (args.root, args.port, args.web_port, args.no_browser, args.paid) == (tmp_path / "data-copy", 8765, 5180, True, True)


def test_paid_is_set_for_the_server_only_when_asked(monkeypatch):
    monkeypatch.delenv("SIM_ALLOW_PAID", raising=False)
    assert "SIM_ALLOW_PAID" not in host.server_env(False), "without --paid the server's own .env decides (DECISIONS-31)"
    assert host.server_env(True)["SIM_ALLOW_PAID"] == "1"
    monkeypatch.setenv("SIM_ALLOW_PAID", "1")
    assert host.server_env(False)["SIM_ALLOW_PAID"] == "1", "an inherited switch is kept, as the server would read it"


def test_key_like_text_is_masked_in_the_logs():
    line = "key sk-abcDEF123456789 auth Bearer abc.def-123456789 api_key=ABCDEFGH12345678 ok\n"
    out = host.mask(line)
    assert "sk-abc" not in out and "abc.def" not in out and "ABCDEFGH" not in out and out.endswith(" ok\n")


def test_the_refusal_line_of_the_server_is_found():
    assert host.refusal(["INFO start\n", "ERROR data_folder_in_use: data/ is used by pid 4242\n"]) == (
        "ERROR data_folder_in_use: data/ is used by pid 4242"
    )
    assert host.refusal(["INFO start\n"]) is None


@pytest.fixture
def busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        yield s.getsockname()[1]


@pytest.mark.parametrize("flag", ["--port", "--web-port"])
def test_a_port_in_use_ends_with_the_reason_and_starts_or_stops_nothing(busy_port, flag, monkeypatch, capsys):
    monkeypatch.setattr(host.shutil, "which", lambda name: f"/bin/{name}")

    def refuse(*a, **k):
        raise AssertionError("no process may start or stop on a busy port")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    other = "--web-port" if flag == "--port" else "--port"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    code = host.main([flag, str(busy_port), other, str(free), "--no-browser"])
    err = capsys.readouterr().err
    assert code == 2
    assert f"Port {busy_port}" in err and "in use by another process" in err and "does not stop it" in err and flag in err


def test_a_missing_tool_is_named(monkeypatch, capsys):
    monkeypatch.setattr(host.shutil, "which", lambda name: None if name == "pnpm" else f"/bin/{name}")
    assert host.main(["--no-browser"]) == 1
    assert "pnpm not found on PATH" in capsys.readouterr().err


def test_missing_node_modules_runs_pnpm_install_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(host, "REPO", tmp_path)
    monkeypatch.setattr(host.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(host, "port_free", lambda port: True)
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert host.main(["--root", str(tmp_path), "--no-browser"]) == 1
    assert ran == [["/bin/pnpm", "--dir", "web", "install"]]
    assert "pnpm --dir web install failed" in capsys.readouterr().err
