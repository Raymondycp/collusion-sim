"""Host the simulator with one command: the API server and the web UI, then the browser. Standard library only.

    uv run python scripts/host.py [--root DIR] [--port 8000] [--web-port 5173] [--no-browser] [--paid]
    host.cmd [same options]   (Windows: double-click, or run from a terminal)
    ./host.sh [same options]  (macOS / Linux, from a terminal)

1. Checks uv and pnpm, and runs `pnpm --dir web install` when web/node_modules is missing.
2. Starts `uv run collusion-sim serve --root ROOT --port PORT`. ROOT is this repo by default, so the server reads
   ROOT/.env and the team data. Paid calls follow the server's own switch (SIM_ALLOW_PAID in the environment or in
   ROOT/.env, DECISIONS-31); --paid sets SIM_ALLOW_PAID=1 for the server process only. The ready line reports the
   switch as the server's /api/health states it.
3. Waits up to 90 s for /api/health. On failure it prints the reason and the server log path.
4. Starts the web dev server (`pnpm --dir web dev --port WEB_PORT`) with SIM_BACKEND set to the server.
5. Opens the browser at the web URL, unless --no-browser.
6. Ctrl-C stops the two processes it started, with their child processes, and nothing else.

A port in use, or a server that refuses with data_folder_in_use (another server on the same data folder), ends the
script with the plain reason and a non-zero exit. It never stops another process. Logs go to ROOT/data/host-logs/,
with key-like text masked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HEALTH_WAIT_S = 90
WEB_WAIT_S = 60
IN_USE = "data_folder_in_use"
SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|(?i:bearer)\s+[A-Za-z0-9._\-]{8,}|(?i:api[_-]?key)[\"'=: ]+[A-Za-z0-9._\-]{8,})")
WINDOWS = os.name == "nt"


class HostError(Exception):
    """A plain reason to stop, printed as one line; the exit code is 1 (2 for a port in use)."""

    def __init__(self, reason: str, code: int = 1) -> None:
        super().__init__(reason)
        self.code = code


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="host.py", description="Start the simulator server and web UI, then open the browser.")
    ap.add_argument("--root", type=Path, default=REPO, help="server data folder (default: this repo, with its .env and team data)")
    ap.add_argument("--port", type=int, default=8000, help="API server port (default 8000)")
    ap.add_argument("--web-port", type=int, default=5173, help="web UI port (default 5173)")
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    ap.add_argument("--paid", action="store_true", help="force paid model calls on (SIM_ALLOW_PAID=1 for the server only); without it the server's .env decides")
    args = ap.parse_args(argv)
    args.root = args.root.resolve()
    return args


def mask(line: str) -> str:
    return SECRET.sub("[masked]", line)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def check_tools() -> tuple[str, str]:
    uv, pnpm = shutil.which("uv"), shutil.which("pnpm")
    missing = [n for n, p in (("uv", uv), ("pnpm", pnpm)) if p is None]
    if missing:
        raise HostError(f"{' and '.join(missing)} not found on PATH. Install it, then run host again.")
    return uv, pnpm


def check_ports(args: argparse.Namespace) -> None:
    for name, port, flag in (("server", args.port, "--port"), ("web", args.web_port, "--web-port")):
        if not port_free(port):
            raise HostError(f"Port {port} ({name}) is in use by another process. host does not stop it. Use {flag} with a free port.", 2)


def server_env(paid: bool) -> dict[str, str]:
    env = dict(os.environ) | {"PYTHONUNBUFFERED": "1"}  # log lines as they come
    if paid:
        env["SIM_ALLOW_PAID"] = "1"
    return env


def paid_state(port: int) -> str:
    """ON or off, as the running server's /api/health states it (it reads the environment, then ROOT/.env)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as r:
            return "ON" if json.loads(r.read()).get("paid_calls_enabled") else "off"
    except (OSError, ValueError):
        return "unknown"


def refusal(log_lines: list[str]) -> str | None:
    """The server's own line when it refused the data folder, else None."""
    return next((ln.strip() for ln in log_lines if IN_USE in ln), None)


class Child:
    """One started process, its masked log file and its last lines."""

    def __init__(self, name: str, cmd: list[str], env: dict[str, str], log: Path) -> None:
        self.name, self.log, self.lines = name, log, []
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0  # Ctrl-C reaches host only; host stops the tree
        self.proc = subprocess.Popen(
            cmd, cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=flags, start_new_session=not WINDOWS,
        )  # fmt: skip
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        with self.log.open("a", encoding="utf-8") as f:
            for raw in self.proc.stdout:
                line = mask(raw)
                self.lines = [*self.lines[-200:], line]
                f.write(line)
                f.flush()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        """Stop this process and its children only (Windows: taskkill /T on this PID; POSIX: its own session)."""
        if not self.alive():
            return
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def wait_health(server: Child, port: int) -> None:
    url, end = f"http://127.0.0.1:{port}/api/health", time.monotonic() + HEALTH_WAIT_S
    while time.monotonic() < end:
        if (why := refusal(server.lines)) is not None:
            raise HostError(f"The server refused the data folder: {why}. Another server uses it; host does not stop it. Log: {server.log}")
        if not server.alive():
            raise HostError(f"The server stopped (exit {server.proc.returncode}) before it answered. Log: {server.log}")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise HostError(f"The server did not answer {url} within {HEALTH_WAIT_S} s. Log: {server.log}")


def wait_port(child: Child, port: int, wait_s: float) -> None:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if not child.alive():
            raise HostError(f"The {child.name} stopped (exit {child.proc.returncode}). Log: {child.log}")
        if not port_free(port):
            return
        time.sleep(0.5)
    raise HostError(f"The {child.name} did not open port {port} within {wait_s:g} s. Log: {child.log}")


def run(args: argparse.Namespace) -> int:
    uv, pnpm = check_tools()
    check_ports(args)
    if not (REPO / "web" / "node_modules").is_dir():
        print("web/node_modules is missing: running pnpm --dir web install", flush=True)
        if subprocess.run([pnpm, "--dir", "web", "install"], cwd=REPO).returncode != 0:
            raise HostError("pnpm --dir web install failed.")
    logs = args.root / "data" / "host-logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    api = f"http://127.0.0.1:{args.port}"
    web_url = f"http://127.0.0.1:{args.web_port}/"
    children: list[Child] = []
    try:
        cmd = [uv, "run", "collusion-sim", "serve", "--root", str(args.root), "--port", str(args.port)]
        server = Child("server", cmd, server_env(args.paid), logs / f"server-{stamp}.log")
        children.append(server)
        print(f"Server starting on {api} (root {args.root}; log {server.log})", flush=True)
        wait_health(server, args.port)
        web_env = {**{k: v for k, v in server_env(False).items() if k != "SIM_ALLOW_PAID"}, "SIM_BACKEND": api}
        web = Child("web dev server", [pnpm, "--dir", "web", "dev", "--port", str(args.web_port)], web_env, logs / f"web-{stamp}.log")
        children.append(web)
        wait_port(web, args.web_port, WEB_WAIT_S)
        paid = paid_state(args.port)
        if paid == "ON":
            print("WARNING: paid model calls are ON for this server. Runs with LLM sellers spend money.", flush=True)
        print(f"Ready: web {web_url}  (API {api}, paid calls {paid}). Ctrl-C stops both.", flush=True)
        if not args.no_browser:
            webbrowser.open(web_url)
        while all(c.alive() for c in children):
            time.sleep(0.5)
        gone = next(c for c in children if not c.alive())
        raise HostError(f"The {gone.name} stopped (exit {gone.proc.returncode}). Log: {gone.log}")
    except KeyboardInterrupt:
        print("Stopping the server and the web UI ...", flush=True)
        return 0
    finally:
        for c in reversed(children):
            c.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    try:
        return run(args)
    except HostError as e:
        print(f"host: {e}", file=sys.stderr, flush=True)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
