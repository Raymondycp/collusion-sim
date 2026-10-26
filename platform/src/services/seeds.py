"""Make the seed map (DECISIONS-10 §2): one frozen scenario per seed from the `random` generator inside fixed bounds,
written to scenarios/scn_s<seed>.json, and conditions/seeds.yaml. Every condition uses the same map (paired design).

    uv run python -m services.seeds make [--root .] [--approved-by NAME] [--market base|elastic]
    uv run python -m services.seeds show [--root .]

`make` is idempotent: an existing scenario file with the same content hash is kept; a different one is refused,
because a frozen scenario never changes. `show` prints the reference values per seed for review.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from services.research import SEEDS_FILE, GeneratorRef, SeedEntry, SeedMap
from simcore.generators import GENERATORS, LINEAR_BASE
from simcore.scenario import Scenario

# Each market has its own seeds and its own data root (one seed map per root). `base` is the main study; `elastic` is the
# Haiku study (user, 25 Oct: close substitutes, higher own-price slope b with b - d = 0.5, so the joint optimum stays 10.5
# inside the grid and the Nash price falls). Test seeds 311-320 added for the frozen-rule test (DECISIONS-36).
MARKETS = {
    "base": {"params": dict(LINEAR_BASE), "splits": {"pilot": [1, 2, 3], "dev": list(range(101, 111)), "test": list(range(201, 211))}},
    "elastic": {"params": {"a": 10.0, "b": 3.0, "d": 2.5, "c": 1.0}, "splits": {"pilot": [31, 32, 33], "dev": list(range(301, 311)), "test": list(range(311, 321))}},
}
SPLITS = MARKETS["base"]["splits"]
# Designed seed factor (CR-6 M1): who is told about the cost shock. Balanced per split; blocks, not seed % 2, so it is
# not tied to the prompt variant. The first half of each split (rounded up) is public.
SHOCK_NEWS = {
    seed: ("public" if i < len(seeds) - len(seeds) // 2 else "private")
    for m in MARKETS.values()
    for seeds in m["splits"].values()
    for i, seed in enumerate(seeds)
}
# Private seeds: one fixed informed seller per seed, alternating A, B across the private seeds of each split (lead, CR-12 m3).
INFORMED = {
    seed: "AB"[i % 2]
    for m in MARKETS.values()
    for seeds in m["splits"].values()
    for i, seed in enumerate(s for s in seeds if SHOCK_NEWS[s] == "private")
}


def bounds(market: str) -> dict:
    return {
        "name": "Seed scenario",
        "n_sellers": 2,
        "min_rounds": 26,
        "max_rounds": 35,
        "stop_prob": 0.2,
        "demand_model": "linear",
        "params": dict(MARKETS[market]["params"]),
        "param_bounds": {"/a": [9.5, 10.5], "/c": [0.9, 1.1]},
        "shock_bounds": [{"path": "/c", "op": "add", "min": -0.5, "max": 0.5}, {"path": "/a", "op": "mul", "min": 0.9, "max": 1.1}],
        "n_events": 2,
        "placebo_share": 0.0,
        "attempts": 20,
        "required_shock_paths": ["/c"],
    }


BOUNDS = bounds("base")


def scenario_id(seed: int) -> str:
    return f"scn_s{seed}"


def generate(seed: int, market: str = "base") -> Scenario:
    draft = GENERATORS.get("random").generate({**bounds(market), "shock_news": SHOCK_NEWS[seed], "informed_seller": INFORMED.get(seed)}, seed)
    if (draft.provenance or {}).get("check_failed"):
        raise ValueError(f"seed {seed}: no sample inside the bounds passes the scenario check")
    draft = draft.model_copy(update={"id": scenario_id(seed), "name": f"Seed scenario {seed}"})
    problems = grid_problems(draft)
    if problems:
        raise ValueError(f"seed {seed}: {problems[0]}")
    return draft


def grid_problems(s: Scenario) -> list[str]:
    """Nash and joint optimum of every round must be inside the price grid (DECISIONS-7)."""
    market, grid = s.market.build(), s.market.grid
    out = []
    for t in range(1, s.horizon.max_rounds + 1):
        ref = market.reference(s.params(t), s.seller_ids)
        for name, v in (("Nash", ref.nash), ("joint optimum", ref.joint_optimum)):
            if not grid.min <= v <= grid.max:
                out.append(f"round {t}: {name} {v} is outside the grid {grid.min}–{grid.max}")
    return out


def make(root: Path, approved_by: str, market: str = "base") -> SeedMap:
    scenarios = root / "scenarios"
    scenarios.mkdir(parents=True, exist_ok=True)
    entries = []
    for split, seeds in MARKETS[market]["splits"].items():
        for seed in seeds:
            draft = generate(seed, market)
            path = scenarios / f"{draft.id}.json"
            if path.exists():
                kept = Scenario.load(path)
                if kept.hash != draft.content_hash():
                    raise SystemExit(f"{path} is frozen with another content hash; a frozen scenario never changes")
            else:
                kept = draft.frozen(approved_by=approved_by)
                kept.write(path)
            entries.append(
                SeedEntry(
                    seed=seed,
                    split=split,
                    shock_news=SHOCK_NEWS[seed],
                    informed_seller=INFORMED.get(seed),
                    scenario_id=kept.id,
                    scenario_hash=kept.hash,
                )
            )
    gen = GENERATORS.get("random")
    seed_map = SeedMap(generator=GeneratorRef(id=gen.id, version=gen.version, inputs=bounds(market)), seeds=entries)
    out = root / "conditions" / SEEDS_FILE
    header = "# Seed map (DECISIONS-10 §2). Written by `uv run python -m services.seeds make`; do not edit by hand.\n"
    text = header + yaml.safe_dump(seed_map.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    return seed_map


def rows(root: Path) -> list[dict]:
    seed_map = SeedMap.model_validate(yaml.safe_load((root / "conditions" / SEEDS_FILE).read_text(encoding="utf-8")))
    out = []
    for e in seed_map.seeds:
        s = Scenario.load(root / "scenarios" / f"{e.scenario_id}.json")
        market = s.market.build()
        refs = [market.reference(s.params(t), s.seller_ids) for t in range(1, s.horizon.max_rounds + 1)]
        p = s.market.demand.params
        out.append(
            {
                "seed": e.seed,
                "split": e.split,
                "news": e.shock_news + (f" {e.informed_seller}" if e.informed_seller else ""),
                "a": p["a"],
                "c": p["c"],
                "nash_1": refs[0].nash,
                "joint_1": refs[0].joint_optimum,
                "nash_range": (min(r.nash for r in refs), max(r.nash for r in refs)),
                "joint_range": (min(r.joint_optimum for r in refs), max(r.joint_optimum for r in refs)),
                "stop_round": s.horizon.stop_round(e.seed),
                "events": len(s.events),
                "shocks": sum(bool(ev.shocks) for ev in s.events),
                "cost_shocks": sum(sh.path == "/c" for ev in s.events for sh in ev.shocks),
                "demand_shocks": sum(sh.path == "/a" for ev in s.events for sh in ev.shocks),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.seeds")
    ap.add_argument("command", choices=["make", "show"])
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--approved-by", default="de (bounds for lead approval, DECISIONS-10 §2)")
    ap.add_argument("--market", choices=sorted(MARKETS), default="base")
    args = ap.parse_args(argv)
    if args.command == "make":
        m = make(args.root, args.approved_by, args.market)
        print(f"{len(m.seeds)} seeds, map hash {m.hash}")
    print("seed split news        a     c  Nash(1) joint(1)  Nash min-max  joint min-max  stop events shocks cost demand")
    for r in rows(args.root):
        print(
            f"{r['seed']:>4} {r['split']:<5} {r['news']:<7} {r['a']:>5} {r['c']:>5} {r['nash_1']:>8.3f} {r['joint_1']:>8.3f}  "
            f"{r['nash_range'][0]:.3f}-{r['nash_range'][1]:.3f}  {r['joint_range'][0]:.3f}-{r['joint_range'][1]:.3f}  "
            f"{r['stop_round']:>4} {r['events']:>6} {r['shocks']:>6} {r['cost_shocks']:>4} {r['demand_shocks']:>6}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
