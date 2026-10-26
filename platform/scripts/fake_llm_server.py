"""The real app with a fake LLM, served for the web UI: LLM sellers, a trail with thinking, no paid call, no key.

Run from the repo root: uv run python scripts/fake_llm_server.py [--port 8017] [--root DIR]
Then, in web/: SIM_BACKEND=http://127.0.0.1:8017 pnpm dev   (PowerShell: $env:SIM_BACKEND = "http://127.0.0.1:8017"; pnpm dev)

The app is `create_app` with the tests' fake transport (tests/fakellm.py), so no request leaves the machine. The server starts with
SIM_ALLOW_PAID=1 in this process only. The root is a temporary folder with a copy of conditions/ and scenarios/ (or --root DIR,
which must hold both). A provider profile "Fake local" is made at start, and its key lives in memory.

The fake seller: its talk call posts one offer, its pricing call sets 10.5 when its prompt holds an offer and 6.0 otherwise, and each
reply carries `reasoning_content`, so a trail has thinking. Used for the web check of FE-realbackend-report.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

DELAY_S = 0.0  # seconds that each model reply takes, so a run lasts long enough to watch live (--delay-ms)
OFFER = "Offer: I will hold 10.5 if you do."
THINKING = "The rival is likely to follow; keep the plan simple."
PROFILE = {
    "name": "Fake local",
    "base_url": "https://llm.example.test/v1",
    "api_key": "sk-FAKE-NOT-A-REAL-KEY-0000000000",
    "default_model": "m-small",
    "pricing": {"input_usd_per_mtok": 0.15, "output_usd_per_mtok": 0.6},
    "max_concurrency": 4,
}


class MemoryKeys:
    """A key store in memory: nothing goes to the OS keyring."""

    def __init__(self) -> None:
        self.d: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.d.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.d[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.d.pop((service, username), None)


def post_channel(user: str) -> str:
    """The first private pair that the prompt lists, else the first channel. The prompt says "Channels you can post to: a, b."."""
    m = re.search(r"Channels you can post to: ([^.\n]+)\.", user)
    ids = [c.strip() for c in m.group(1).split(",")] if m else ["public"]
    return next((c for c in ids if c.startswith("private_")), ids[0])


def brain(body: dict) -> dict:
    """The reply to one chat request. The tools in the request say which call it is."""
    from fakellm import call, text

    if DELAY_S:
        time.sleep(DELAY_S)
    tools = [t["function"]["name"] for t in body.get("tools") or []]
    user = next((m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), "")
    if "set_price" in tools:
        price = 10.5 if "Offer" in user else 6.0
        reply = call("set_price", price=price, plan="Follow the rival offer." if price > 6 else "Stay at the competitive price.")
    elif "post" in tools:
        reply = call("post", channel_id=post_channel(user), text=OFFER)
    elif "ping" in tools:  # the profile probe (run-1c.sh preflight needs a tool call)
        reply = call("ping", ok=True)
    else:
        reply = text(OFFER)
    return {**reply, "reasoning_content": THINKING}


def temp_root(root: Path | None) -> Path:
    """A root that holds conditions/ and scenarios/: the given one, or a temporary copy of the repo's."""
    if root is not None:
        missing = [d for d in ("conditions", "scenarios") if not (root / d).is_dir()]
        if missing:
            raise SystemExit(f"{root} has no {' and no '.join(missing)} folder.")
        return root
    root = Path(tempfile.mkdtemp(prefix="fake-llm-root-"))
    for d in ("conditions", "scenarios"):
        shutil.copytree(ROOT / d, root / d)
    return root


@contextmanager
def fake_root(root: Path | None) -> Iterator[Path]:
    """The root to serve. A given root is kept. A temporary copy is removed when the server stops (CR-87 minor 4)."""
    if root is not None:
        yield temp_root(root)
        return
    made = temp_root(None)
    try:
        yield made
    finally:
        shutil.rmtree(made, ignore_errors=True)


@contextmanager
def paid_switch() -> Iterator[None]:
    """SIM_ALLOW_PAID=1 while the server runs, then as it was, so a caller that runs `main` does not leak it (CR-87 minor 3)."""
    before = os.environ.get("SIM_ALLOW_PAID")
    os.environ["SIM_ALLOW_PAID"] = "1"
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("SIM_ALLOW_PAID", None)
        else:
            os.environ["SIM_ALLOW_PAID"] = before


def build_app(root: Path):
    """The real app on `root` with the fake transport and an in-memory key store. Not durable (no DBOS)."""
    from api.app import create_app
    from fakellm import FakeLLM

    return create_app(root, use_dbos=False, key_backend=MemoryKeys(), llm_transport=FakeLLM(brain).transport)


def make_profile(base: str, wait_s: float = 60) -> str:
    """Waits for the server, then makes the profile. Returns its id."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base}/api/health", timeout=2).read()
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    req = urllib.request.Request(
        f"{base}/api/provider-profiles", data=json.dumps(PROFILE).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["id"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8017)
    ap.add_argument("--delay-ms", type=lambda v: max(0, int(v)), default=0, help="each model reply takes this long, so a run can be watched live (default 0)")
    ap.add_argument("--root", type=Path, default=None, help="a folder with conditions/ and scenarios/ (default: a temporary copy)")
    args = ap.parse_args()
    import uvicorn

    global DELAY_S
    DELAY_S = args.delay_ms / 1000
    base = f"http://127.0.0.1:{args.port}"
    with fake_root(args.root) as root, paid_switch():

        def announce() -> None:
            pid = make_profile(base)
            print(f"Fake LLM server ready: {base}  (root {root}, profile {pid}, no paid call)", flush=True)
            print("Web UI: in web/ run  SIM_BACKEND=" + base + " pnpm dev", flush=True)

        threading.Thread(target=announce, daemon=True).start()
        uvicorn.run(build_app(root), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
