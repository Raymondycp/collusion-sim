"""Calls and tokens of the full pre-registered plan, from the running server's estimate routes (acceptance A6). No LLM call.

    uv run python scripts/plan_estimate.py (--tokens-per-call IN,OUT | --talk-tokens IN,OUT --act-tokens IN,OUT)
        [--url URL] [--profile PROF --model MODEL]
        [--conditions C1,C2a,C3-accept] [--seeds-per-split 10] [--mean-rounds 30] [--retry-margin 0.1]
        (--judge-tokens IN,OUT | --talk-only-tokens IN,OUT --talk-prices-tokens IN,OUT)
        [--check-rounds 2] [--price IN,OUT] [--judge-price IN,OUT] [--budget-usd USD] [--json]

The plan: each condition on 10 dev and 10 test seeds (paired); per cell 1 probe child and 3 placebo children (round 20,
5 rounds each) and, for groups C1 and C3, one ablation child per private channel from round 10 to the parent's end.
Calls follow DECISIONS-13 §1: an LLM seller makes 2 calls per round when its condition gives it a talk call, else 1.
Expected calls = those calls x (1 + retry margin). Tokens = talk calls x --talk-tokens + act calls x --act-tokens (input,
output per call: the means of the first D13 C1c run, not earlier C1c runs); --tokens-per-call sets both.
A one-call turn counts as an act call.

Beside the sellers, one total over the whole plan (CR-62 finding 2):
- The LLM judge, one backend (jev is designed, not built). The blocks come from the server (GET /api/judge/blocks): a block
  on dev or test seeds judges every planned parent episode of its splits and of its conditions (`conditions`, null for
  any; DECISIONS-13 §7), x its paraphrases; stability and robustness judge a fixed sample of 10 episodes; the pilot wording
  block counts its cap. Plus the server's retry reserve (GET
  /api/judge/ledger). A block whose planned calls pass its server cap is named under the table. Tokens per call by view
  (input: the rendered transcript, `tokens_est` of GET /runs/{id}/transcript?view=...): --talk-only-tokens and
  --talk-prices-tokens, the longer view with its price table (CR-64 finding 4); --judge-tokens sets both. A block with both
  views (wording development) and the retry reserve count at the larger pair.
- The model checks (DECISIONS-13 §2): one before each paid split (dev, test) for each seller model and settings, each
  --check-rounds rounds of C1 at its D13 counts, with the retry margin.

The sellers of each condition come from the server's conditions/models.yaml (GET /api/research/models), as dev and test
runs must use them; --profile and --model give every seller one LLM instead (before models.yaml is filled).
From the server: per condition, which sellers get a talk call (POST /runs/estimate, calls by phase) and the children per
cell (POST /experiments/estimate); per seed, its stop round (POST /runs/estimate). A split that the server keeps closed
(dev before stage `conditions`: condition_not_frozen; test before stage `rule`: test_split_locked) takes its cell
structure from a pilot seed and --mean-rounds rounds per parent; its rows are marked `assumed`. Any other refusal stops the
script. The server's own worst case (every tool step and attempt) is printed beside.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

import yaml

Api = Callable[[str, str, "dict | None"], "tuple[int, object]"]
"""(method, path under /api, JSON body or None) -> (HTTP status, JSON body)."""

CONDITIONS = ["C1", "C2a", "C3-accept"]
"""The registered main plan: the brief's three case types (DECISIONS-16; C3-silent is an extension, designed not run)."""
BRANCH_PLAN = {
    "probe": {"at": 20, "rounds": 5, "target": "B"},
    "placebo": {"at": 20, "rounds": 5, "n": 3},
    "ablation": {"at": 10, "mode": "shuffle", "groups": ["C1", "C3"]},
}
CLOSED = {"condition_not_frozen", "test_split_locked"}  # the dev and test gates (services/research.py)
JUDGE_SAMPLE = {"stability": 10, "robustness": 10}  # DECISIONS-9 §18: fixed samples; other dev/test blocks judge every parent
PAID_SPLITS = ("dev", "test")
LLM_KEYS = (
    "provider_profile_id",
    "model",
    "model_family",
    "temperature",
    "top_p",
    "max_output_tokens",
    "timeout_s",
    "thinking",
    "thinking_on_timeout",
)  # services/models_file.py


@dataclass
class Group:
    condition_id: str
    talk_per_round: float = 0.0  # D13: talk calls per round of one run, summed over the LLM sellers
    act_per_round: float = 0.0  # act (or one-call) calls per round: one per LLM seller
    cells: int = 0
    runs: int = 0
    run_rounds: int = 0  # parents' and children's rounds
    server_calls_max: int = 0
    assumed: list[str] = field(default_factory=list)  # splits whose rounds come from --mean-rounds


def http_api(url: str) -> Api:
    def call(method: str, path: str, body: dict | None) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw.decode(errors="replace")

    return call


def fail(what: str, status: int, body: object) -> SystemExit:
    return SystemExit(f"plan_estimate: {what}: HTTP {status} {json.dumps(body)[:400]}")


def get(api: Api, path: str) -> object:
    status, body = api("GET", path, None)
    if status != 200:
        raise fail(f"GET {path}", status, body)
    return body


def closed(status: int, body: object) -> bool:
    return status == 409 and isinstance(body, dict) and body.get("code") in CLOSED


def seller_agents(api: Api, conditions: list[str], profile: str | None, model: str | None) -> dict[str, list[dict]]:
    """Agents per condition: one LLM for every seller (--profile, --model), else the sellers of models.yaml."""
    if profile:
        llm = {"kind": "llm", "llm": {"provider_profile_id": profile, "model": model}}
        return {c: [{"id": s, "role": "seller", "policy": llm} for s in "AB"] for c in conditions}
    text = get(api, "/research/models").get("text")
    if not text:
        raise SystemExit("plan_estimate: the server has no conditions/models.yaml; write it, or pass --profile and --model")
    doc = yaml.safe_load(text)
    out = {}
    for c in conditions:
        sellers = doc["sellers"] | doc.get("by_condition", {}).get(c, {})
        out[c] = [{"id": s, "role": "seller", "policy": policy(e)} for s, e in sorted(sellers.items())]
    return out


def policy(entry: dict | str) -> dict:
    """A models.yaml seller entry as an agent policy; a scripted seller makes no calls, so any scripted kind will do."""
    if entry == "scripted":
        return {"kind": "constant", "params": {"price": 10.5}}
    return {"kind": "llm", "llm": {k: entry[k] for k in LLM_KEYS}}


def run_estimate(api: Api, cid: str, split: str, seed: int, agents: list[dict]) -> tuple[int, object]:
    body = {"kind": "research", "name": "plan estimate", "condition_id": cid, "split": split, "seeds": [seed], "agents": agents,
            "clock": {"mode": "sync"}, "budget": {"max_usd": 1e6}}  # fmt: skip
    return api("POST", "/runs/estimate", body)


def experiment_estimate(api: Api, cid: str, split: str, seeds: list[int], agents: list[dict], plan: dict) -> tuple[int, object]:
    body = {"name": "plan estimate", "conditions": [cid], "split": split, "seeds": seeds, "agents": agents, "branch_plan": plan,
            "budget": {"max_usd": 1e6}, "batch_budget": {"max_calls": 10**12}}  # fmt: skip
    return api("POST", "/experiments/estimate", body)


def estimate(api: Api, seeds: dict[str, list[int]], agents: dict[str, list[dict]], plan: dict, mean_rounds: int) -> list[Group]:
    """One Group per condition over its dev and test cells."""
    conditions, pilot = list(agents), seeds["pilot"][0]
    stops: dict[str, list[int] | None] = {}
    for split in ("dev", "test"):  # a seed's stop round is the same in every condition (paired seeds)
        got: list[int] | None = []
        for s in seeds[split]:
            status, body = run_estimate(api, conditions[0], split, s, agents[conditions[0]])
            if closed(status, body):
                got = None
                break
            if status != 200:
                raise fail(f"runs estimate {conditions[0]} {split} seed {s}", status, body)
            got.append(body["per_run"]["max_rounds"])
        stops[split] = got
    out = []
    for cid in conditions:
        g, sellers = Group(cid), agents[cid]
        status, body = run_estimate(api, cid, "pilot", pilot, sellers)
        if status != 200:
            raise fail(f"runs estimate {cid} pilot seed {pilot}", status, body)
        ph = body["per_run"]["calls"]
        turns = ph["act"] + ph["one_call"]  # one per LLM seller and round
        g.act_per_round = sum(a["policy"]["kind"] == "llm" for a in sellers) if turns else 0
        g.talk_per_round = g.act_per_round * ph["talk"] / turns if turns else 0.0
        for split in ("dev", "test"):
            rounds = stops[split]
            if rounds is None:
                g.assumed.append(split)
                rounds = [mean_rounds] * len(seeds[split])
                status, body = experiment_estimate(api, cid, "pilot", [pilot], sellers, plan)
                scale = len(rounds)  # the pilot cell's worst case, once per planned seed
            else:
                status, body = experiment_estimate(api, cid, split, seeds[split], sellers, plan)
                scale = 1
            if status != 200:
                raise fail(f"experiments estimate {cid} {split}", status, body)
            per_cell = {k: body["runs"][k] // body["n_cells"] for k in ("probe", "placebo", "ablation")}
            fixed = per_cell["probe"] * plan["probe"]["rounds"] + per_cell["placebo"] * plan["placebo"]["rounds"]
            for stop in rounds:
                g.run_rounds += stop + fixed + per_cell["ablation"] * (stop - plan["ablation"]["at"] + 1)
            g.cells += len(rounds)
            g.runs += len(rounds) * (1 + sum(per_cell.values()))
            g.server_calls_max += body["calls_max"] * scale
        out.append(g)
    return out


Tokens = tuple[float, float]  # (input, output) per call


def seller_rows(groups: list[Group], margin: float, talk: Tokens, act: Tokens) -> list[dict]:
    rows = []
    for g in groups:
        talk_calls = g.talk_per_round * g.run_rounds * (1 + margin)
        act_calls = g.act_per_round * g.run_rounds * (1 + margin)
        rows.append({"condition_id": g.condition_id, "cells": g.cells, "runs": g.runs, "run_rounds": g.run_rounds,
                     "talk_per_round": g.talk_per_round, "act_per_round": g.act_per_round,
                     "calls_d13": round((g.talk_per_round + g.act_per_round) * g.run_rounds), "talk_calls": round(talk_calls),
                     "act_calls": round(act_calls), "calls": round(talk_calls + act_calls),
                     "input_tokens": round(talk_calls * talk[0] + act_calls * act[0]),
                     "output_tokens": round(talk_calls * talk[1] + act_calls * act[1]),
                     "server_calls_max": g.server_calls_max, "assumed": g.assumed})  # fmt: skip
    return rows


def judge_row(api: Api, conditions: list[str], seeds: dict[str, int], tokens: dict[str, Tokens]) -> dict:
    """The LLM judge's calls over the plan's blocks; `over_cap` names each block whose plan passes its server cap."""
    blocks, ledger = get(api, "/judge/blocks"), get(api, "/judge/ledger")
    first, over, tok_in, tok_out = 0, [], 0, 0
    upper = max(tokens.values())
    for b in (b for b in blocks if b.get("in_plan", True)):  # imported logs are outside the plan (unit 5b)
        if set(b["splits"]) <= set(PAID_SPLITS):
            mine = [c for c in conditions if b["conditions"] is None or c in b["conditions"]]
            episodes = JUDGE_SAMPLE.get(b["id"], len(mine) * sum(seeds[s] for s in b["splits"]))
            calls = episodes * len(b["paraphrases"])
        else:  # wording development on pilot seeds: a budget, not a function of the plan
            calls = b["cap_per_backend"]
        if calls > b["cap_per_backend"]:
            over.append(f"judge block {b['id']}: {calls} planned calls, server cap {b['cap_per_backend']} per backend")
        first += calls
        per_call = max(tokens[v] for v in b["views"])
        tok_in, tok_out = tok_in + calls * per_call[0], tok_out + calls * per_call[1]
    calls = first + ledger["retry_reserve"]
    tok_in, tok_out = tok_in + ledger["retry_reserve"] * upper[0], tok_out + ledger["retry_reserve"] * upper[1]
    return {"condition_id": "judge (llm)", "calls_d13": first, "calls": calls, "input_tokens": round(tok_in),
            "output_tokens": round(tok_out), "server_calls_max": ledger["cap_per_backend"], "assumed": [],
            "over_cap": over}  # fmt: skip


def check_row(groups: list[Group], models: int, rounds: int, margin: float, talk: Tokens, act: Tokens) -> dict:
    """DECISIONS-13 §2: a model check before each paid split, per seller model; each is `rounds` rounds of C1."""
    c1 = next((g for g in groups if g.condition_id == "C1"), groups[0])
    n = models * len(PAID_SPLITS)
    talk_calls = n * rounds * c1.talk_per_round * (1 + margin)
    act_calls = n * rounds * c1.act_per_round * (1 + margin)
    return {"condition_id": f"model checks ({n})", "calls_d13": round(n * rounds * (c1.talk_per_round + c1.act_per_round)),
            "talk_calls": round(talk_calls), "act_calls": round(act_calls), "calls": round(talk_calls + act_calls),
            "input_tokens": round(talk_calls * talk[0] + act_calls * act[0]),
            "output_tokens": round(talk_calls * talk[1] + act_calls * act[1]), "assumed": []}  # fmt: skip


SUMMED = ("cells", "runs", "run_rounds", "calls_d13", "talk_calls", "act_calls", "calls", "input_tokens", "output_tokens",
          "server_calls_max")  # fmt: skip


def with_total(rows: list[dict], price: Tokens | None, judge_price: Tokens | None) -> list[dict]:
    """Every row gets every column (None where it does not apply) and USD; one total row over all."""
    for r in rows:
        for k in SUMMED:
            r.setdefault(k, None)
        p = judge_price if r["condition_id"].startswith("judge") else price
        if p:
            r["usd"] = round((r["input_tokens"] * p[0] + r["output_tokens"] * p[1]) / 1e6, 2)
    total = {k: sum(r[k] or 0 for r in rows) for k in SUMMED}
    if price:
        total["usd"] = round(sum(r["usd"] for r in rows), 2)
    return rows + [{"condition_id": "total", **total, "assumed": sorted({s for r in rows for s in r["assumed"]})}]


def seller_models(agents: dict[str, list[dict]]) -> int:
    """Distinct seller LLMs with their settings: each needs its own model check."""
    llm = {json.dumps(a["policy"]["llm"], sort_keys=True) for cell in agents.values() for a in cell if a["policy"]["kind"] == "llm"}
    return len(llm)


def text(rows: list[dict], args: argparse.Namespace) -> str:
    cols = [
        "condition_id",
        "cells",
        "runs",
        "run_rounds",
        "calls_d13",
        "talk_calls",
        "act_calls",
        "calls",
        "input_tokens",
        "output_tokens",
        "server_calls_max",
    ]
    cols += ["usd"] * bool(args.price) + ["assumed"]

    def cell(r: dict, c: str) -> str:
        return ", ".join(r[c]) if c == "assumed" else "-" if r[c] is None else str(r[c])

    lines = [" | ".join(cols)] + [" | ".join(cell(r, c) for c in cols) for r in rows]
    jt = args.judge_tokens
    lines.append(f"calls = D13 calls x {1 + args.retry_margin:g} (retry margin); tokens per talk call {args.talk_tokens[0]:g} in, "
                 f"{args.talk_tokens[1]:g} out; per act call {args.act_tokens[0]:g} in, {args.act_tokens[1]:g} out. "
                 f"Judge per call: talk_only {jt['talk_only'][0]:g} in, {jt['talk_only'][1]:g} out; "
                 f"talk_prices {jt['talk_prices'][0]:g} in, {jt['talk_prices'][1]:g} out "
                 "(wording and the reserve at the larger); its calls_d13 = first calls, calls adds "
                 "the server's retry reserve. server_calls_max = the server's worst case (sellers: every tool step and attempt; "
                 "judge: its cap per backend).")  # fmt: skip
    lines += [w for r in rows for w in r.get("over_cap", [])]
    if rows[-1]["assumed"]:
        lines.append(f"assumed: the server keeps split(s) {', '.join(rows[-1]['assumed'])} closed (freeze not done); their parents "
                     f"last {args.mean_rounds} rounds, cell structure from a pilot seed.")  # fmt: skip
    if args.budget_usd is not None and args.price:
        fits = rows[-1]["usd"] <= args.budget_usd
        lines.append(f"budget: {rows[-1]['usd']} USD of {args.budget_usd:g} USD: {'fits' if fits else 'DOES NOT FIT'}")
    return "\n".join(lines)


def pair(s: str) -> tuple[float, float]:
    a, b = s.split(",")
    return float(a), float(b)


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default=os.environ.get("SIM_API_URL", "http://127.0.0.1:8000/api"))
    ap.add_argument("--tokens-per-call", type=pair, metavar="IN,OUT", help="mean tokens per call, for talk and act calls both")
    ap.add_argument("--talk-tokens", type=pair, metavar="IN,OUT", help="mean tokens per talk call (first D13 C1c run)")
    ap.add_argument("--act-tokens", type=pair, metavar="IN,OUT", help="mean tokens per act call (first D13 C1c run)")
    ap.add_argument("--profile", help="one LLM for every seller instead of models.yaml (with --model)")
    ap.add_argument("--model")
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--seeds-per-split", type=int, default=10)
    ap.add_argument("--mean-rounds", type=int, default=30, help="rounds per parent for a split that the server keeps closed")
    ap.add_argument("--retry-margin", type=float, default=0.1)
    ap.add_argument("--judge-tokens", type=pair, metavar="IN,OUT", help="mean tokens per judge call, both views")
    ap.add_argument("--talk-only-tokens", type=pair, metavar="IN,OUT", help="mean tokens per judge call, view talk_only")
    ap.add_argument("--talk-prices-tokens", type=pair, metavar="IN,OUT", help="mean tokens per judge call, view talk_prices")
    ap.add_argument("--check-rounds", type=int, default=2, help="C1 rounds per model check (run-1c.sh stage check)")
    ap.add_argument("--price", type=pair, metavar="IN,OUT", help="USD per million tokens")
    ap.add_argument("--judge-price", type=pair, metavar="IN,OUT", help="USD per million judge tokens (default: --price)")
    ap.add_argument("--budget-usd", type=float)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if bool(args.profile) != bool(args.model):
        ap.error("--profile and --model go together")
    args.judge_price = args.judge_price or args.price
    args.judge_tokens = {
        "talk_only": args.talk_only_tokens or args.judge_tokens,
        "talk_prices": args.talk_prices_tokens or args.judge_tokens,
    }
    if not all(args.judge_tokens.values()):
        ap.error("give --judge-tokens, or --talk-only-tokens and --talk-prices-tokens")
    args.talk_tokens = args.talk_tokens or args.tokens_per_call
    args.act_tokens = args.act_tokens or args.tokens_per_call
    if not (args.talk_tokens and args.act_tokens):
        ap.error("give --tokens-per-call, or --talk-tokens and --act-tokens")
    return args


def main(argv: list[str], api: Api | None = None) -> int:
    args = parse(argv)
    api = api or http_api(args.url)
    listed = get(api, "/seeds")
    entries = listed.get("seeds", listed) if isinstance(listed, dict) else listed
    seeds = {sp: [e["seed"] for e in entries if e.get("split") == sp] for sp in ("pilot", "dev", "test")}
    for sp in ("dev", "test"):
        if len(seeds[sp]) < args.seeds_per_split:
            raise SystemExit(f"plan_estimate: the seed map has {len(seeds[sp])} {sp} seeds, the plan needs {args.seeds_per_split}")
        seeds[sp] = seeds[sp][: args.seeds_per_split]
    agents = seller_agents(api, args.conditions.split(","), args.profile, args.model)
    groups = estimate(api, seeds, agents, BRANCH_PLAN, args.mean_rounds)
    rows = seller_rows(groups, args.retry_margin, args.talk_tokens, args.act_tokens)
    rows += [
        judge_row(api, list(agents), {s: len(seeds[s]) for s in PAID_SPLITS}, args.judge_tokens),
        check_row(groups, seller_models(agents), args.check_rounds, args.retry_margin, args.talk_tokens, args.act_tokens),
    ]
    rows = with_total(rows, args.price, args.judge_price)
    print(json.dumps(rows, indent=1) if args.json else text(rows, args))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
