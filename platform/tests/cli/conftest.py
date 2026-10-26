"""CLI tests: the real app in-process (TestClient), scripted runs only, no DBOS, no network."""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from api.app import create_app
from simcli.main import cli
from condcopy import draft_conditions

ROOT = Path(__file__).parents[2]
SCENARIO = json.loads((ROOT / "scenarios" / "scn_mock_demo.json").read_text(encoding="utf-8"))


def scripted_config(seeds=(1,)) -> dict:
    grants = [{"tool_id": t} for t in ("get_news", "get_own_sales", "set_price", "hold")]
    grants += [
        {"tool_id": "get_rival_prices", "params": {"depth": 5}},
        {"tool_id": "read_channel", "params": {"channels": ["private_A_B"]}},
        {"tool_id": "post", "params": {"channels": ["private_A_B"]}},
    ]
    cap = {"grants": grants, "max_queries_per_round": 3}
    return {
        "kind": "exploratory",
        "name": "cli test",
        "scenario_id": SCENARIO["id"],
        "scenario_hash": SCENARIO["hash"],
        "seeds": list(seeds),
        "agents": [
            {
                "id": "A",
                "role": "seller",
                "policy": {"kind": "scripted_talker", "params": {"price": 10.5, "offer_text": "Hold 10.5.", "channel_id": "private_A_B"}},
                "capability": cap,
            },
            {
                "id": "B",
                "role": "seller",
                "policy": {"kind": "grim_trigger", "params": {"target": 10.5, "punish_rounds": 3}},
                "capability": cap,
            },
        ],
        "channels": {"private_pairs": True},
        "call_mode": "talk_and_act",
        "clock": {"mode": "sync"},
        "budget": {"max_usd": 1.0},
    }


@dataclass
class Result:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def json(self):
        return json.loads(self.stdout)


class MemoryKeyring:
    """Keyring backend for tests: the real OS keyring is never touched."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        del self.data[(service, username)]


def invoke(http, *args: str, sleep=None, clock=None, input: str | None = None) -> Result:
    obj = {"http": http, **({"sleep": sleep} if sleep else {}), **({"clock": clock} if clock else {})}
    r = CliRunner().invoke(cli, list(args), obj=obj, input=input, catch_exceptions=False)
    return Result(r.exit_code, r.stdout, r.stderr)


@dataclass
class Server:
    http: TestClient
    run_ids: list[str]
    children: list[str]
    root: Path
    ring: MemoryKeyring

    def __call__(self, *args: str, **kw) -> Result:
        return invoke(self.http, *args, **kw)


@pytest.fixture
def run_cli():
    """invoke(http, *args): the CLI against any httpx2 client (for fake transports)."""
    return invoke


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """One app per module with two finished scripted runs (seeds 1, 2) and two probe children of the first. Tests only read."""
    root = tmp_path_factory.mktemp("simroot")
    shutil.copytree(ROOT / "scenarios", root / "scenarios")
    draft_conditions(root / "conditions")  # CR-11 m1
    ring = MemoryKeyring()
    with TestClient(create_app(root, use_dbos=False, key_backend=ring), base_url="http://testserver/api") as http:
        svc = http.app.state.svc
        run_ids = http.post("runs", json=scripted_config(seeds=(1, 2))).json()["run_ids"]
        for rid in run_ids:
            svc.wait(rid, timeout=30)
        probe = {"at": 15, "rounds": 5, "n": 2, "intervention": {"type": "probe", "target": "A", "price": "nash", "rounds": 1}}
        children = http.post(f"runs/{run_ids[0]}/branches", json=probe).json()["run_ids"]
        for rid in children:
            svc.wait(rid, timeout=30)
        yield Server(http, run_ids, children, root, ring)
