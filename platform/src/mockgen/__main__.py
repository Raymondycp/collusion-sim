"""Mock episodes for fe: a scripted run and one probe branch, made by the real engine.

    uv run python -m mockgen [out_dir]      (default: docs/sim/api/examples)

It also writes scenarios/scn_mock_demo.json, the frozen demo scenario the API serves (not for research).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from simcore.agents import PolicyContext
from simcore.branch import BranchRequest, plan_branches
from simcore.recorder import MemorySink, Recorder
from simcore.run import Run, RunSpec
from simcore.scenario import DemandRef, Event, Horizon, MarketSpec, Scenario
from simcore.scripted import make_scripted
from simcore.shocks import Shock
from simschema.episode import episode_lines
from simschema.events import AgentHeader, Capability, ProbeIntervention, ToolGrant

OUT = Path("docs/sim/api/examples")
PARENT_ID, CHILD_ID = "run_0000000000000000DEM0PARENT", "run_0000000000000000DEM0PR0BE0"
BATCH_ID = "batch_0000000000000000DEM0BATCH0"  # ids follow the contract patterns (26 Crockford base32 characters)
START = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
JOINT = 10.5
POLICIES = {
    "A": ("scripted_talker", {"price": JOINT, "offer_text": "Let us both hold 10.5 and not undercut.", "channel_id": "private_A_B"}),
    "B": ("grim_trigger", {"target": JOINT, "punish_rounds": 3}),
}

README = """# Example episodes (mock data for fe)

Made by `uv run python -m mockgen` with the real engine and scripted agents. No LLM call. Illustrative, not results.

- `episode-demo.jsonl`: 30 rounds, exploratory. A posts the same offer on `private_A_B` each round and always prices 10.5.
  B is a grim trigger with target 10.5. A cost shock (c + 0.5) is announced at round 8 and acts in rounds 10–14.
- `episode-demo-probe.jsonl`: branch of the run above at round 15 (`branch.parent_run_id`).
  A is forced to the Nash price for one round. B punishes in rounds 16–18, then returns to 10.5.

Both examples are scripted runs, so they have no `talk_replied`, `talk_text` or `llm_call` events.
Those event types appear only in the schema examples of `openapi.yaml`.

Line 1 is the `EpisodeHeader`. Each other line is one `SimEvent` (seq 0 … n). Wall times are fixed, so the files do not change between runs.
"""


def _clock(offset: timedelta):
    ticks = iter(range(10**9))
    return lambda: (START + offset + timedelta(milliseconds=40 * next(ticks))).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _scenario() -> Scenario:
    shock = Event(
        announce=8,
        effect=10,
        length=5,
        shocks=[Shock(path="/c", op="add", value=0.5)],
        visible=["A", "B"],
        news="Wholesale fuel costs rise by 0.50 per unit from round 10 for about five rounds.",
    )
    return Scenario(
        id="scn_mock_demo",
        name="Mock demo market",
        description="Demo for the UI and tests. Not a research scenario.",
        market=MarketSpec(demand=DemandRef(id="linear", params={"a": 10, "b": 1.5, "d": 1.0, "c": 1.0})),
        n_sellers=2,
        horizon=Horizon(min_rounds=30, max_rounds=30, stop_prob=0),
        events=[shock],
    ).frozen(approved_by="mockgen")


def _capability() -> Capability:
    return Capability(
        grants=[
            ToolGrant(tool_id="get_rival_prices", params={"depth": 5}),
            ToolGrant(tool_id="get_news"),
            ToolGrant(tool_id="read_channel", params={"channels": ["private_A_B"]}),
            ToolGrant(tool_id="get_own_sales"),
            ToolGrant(tool_id="set_price"),
            ToolGrant(tool_id="hold"),
            ToolGrant(tool_id="post", params={"channels": ["private_A_B"]}),
        ],
        max_queries_per_round=3,
    )


def _policies(scenario: Scenario):
    market = scenario.market.build()
    return {
        s: make_scripted(k, PolicyContext(seller_id=s, sellers=tuple(POLICIES), market=market, params=scenario.params), **p)
        for s, (k, p) in POLICIES.items()
    }


def generate(out: Path) -> None:
    scenario = _scenario()
    spec = RunSpec(
        run_id=PARENT_ID,
        batch_id=BATCH_ID,
        kind="exploratory",
        split="exploratory",
        seed=7,
        private_pairs=True,
        agents=[AgentHeader(id=s, role="seller", policy_kind=k, capability=_capability()) for s, (k, _) in POLICIES.items()],
        created_at=START.isoformat().replace("+00:00", "Z"),
    )
    parent = MemorySink()
    Run(spec, scenario, _policies(scenario), Recorder(PARENT_ID, [parent], now=_clock(timedelta()))).execute()

    request = BranchRequest(at=15, rounds=8, intervention=ProbeIntervention(target="A"))
    (child_plan,) = plan_branches(
        spec, scenario, parent.snapshots, request, [CHILD_ID], created_at=(START + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    )
    child = MemorySink()
    child_plan.run(scenario, _policies(scenario), Recorder(CHILD_ID, [child], now=_clock(timedelta(hours=1)))).execute()

    out.mkdir(parents=True, exist_ok=True)
    for name, sink in (("episode-demo.jsonl", parent), ("episode-demo-probe.jsonl", child)):
        (out / name).write_text("\n".join(episode_lines(sink.events)) + "\n", encoding="utf-8", newline="\n")
    (out / "README.md").write_text(README, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    generate(target)
    _scenario().write(Path("scenarios") / "scn_mock_demo.json")
    print(f"wrote {target} and scenarios/scn_mock_demo.json")
