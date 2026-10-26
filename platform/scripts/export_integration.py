"""Export integration check: a real server on a temporary root, scripted sellers and judge, then the P2 and figure scripts
on the real feature export. No paid calls. Writes every file it makes under OUT (default: a new temporary folder).

It needs the server of contract 0.22.1 or later (kind tags, conditions/models.yaml, probe children with their branch).
Run from the repo root, alone (memory is low): uv run python scripts/export_integration.py [--out DIR] [--seeds 2]
It prints each step and the output of analysis/p2_starter.py and figures/make_all.py, checks the shape of the JSON export
(columns, meta, labels) and exits 1 when a step fails or a consumer script exits non-zero (CR-45). Put OUT outside the
repo or in a git-ignored folder: it holds exports and per-round data.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v1_smoke as smoke  # noqa: E402 (helpers: server, CLI and API calls on a clean environment)

REPO = smoke.REPO
P2 = ["uv", "run", "--no-project", "--with", "pandas", "--with", "numpy", "--with", "scipy", "--with", "pyyaml", "python",
      "analysis/p2_starter.py"]  # fmt: skip
FIGS = ["uv", "run", "--no-project", "--with", "matplotlib", "--with", "numpy", "--with", "scikit-learn", "python", "figures/make_all.py"]

# A decision-rule file that P2 accepts: the starter reads its manipulation_check field (fe, CR-40 fixes). JSON is YAML.
DECISION_RULE = json.dumps({
    "id": "rule-export-integration",
    "rule": "delta_p > 0.5",
    "manipulation_check": {
        "C1": [{"feature": "delta_p", "operator": "<", "value": 0.2}],
        "C3-accept": [{"feature": "delta_p", "operator": ">", "value": 0.6}],
        "C3-silent": [{"feature": "delta_p", "operator": ">", "value": 0.6}],
    },
}, indent=1)  # fmt: skip
ID_COLUMNS = [  # the export's id columns in order (src/services/features.py, contract 0.19.0)
    "experiment_id", "condition_id", "group", "sub_condition", "label_legal", "label_harm", "split", "seed", "parent_run_id",
    "parent_status", "child_run_ids", "child_kinds", "config_hash", "condition_hash", "seeds_hash",
]  # fmt: skip
FEATURES = {"delta_p", "br_gap_net", "msg_volume", "corr_raw", "corr_lag", "corr_cond", "punishment_depth", "ablation_effect"}
SPEC_KEYS = {"id", "version", "name", "description", "evidence_type", "data_layer", "access", "inputs", "scenario", "branches",
             "code_hash"}  # fmt: skip
SUB = {"C3-accept": "responder_accepts", "C3-silent": "responder_silent"}
FAILS: list[str] = []
# children per cell of smoke.BRANCHES: the probe, the placebo samples, and one ablation child (2 sellers: one private pair,
# or a null ablation where the condition has none)
KIDS = bool(smoke.BRANCHES.get("probe")) + smoke.BRANCHES.get("placebo", {}).get("n", 1) + bool(smoke.BRANCHES.get("ablation"))

OFFER = "agent A: let us both hold 10.5 from now on. Reply on private_A_B if you agree."
ACCEPT = "agent B: ok, 10.5 it is. Keep it on private_A_B."
TALKER = {"kind": "scripted_talker", "params": {"price": 10.5, "offer_text": OFFER, "channel_id": "private_A_B"}}
POLICIES = {  # per condition, as tests/test_judge.py _talkers: C2a one kind for both sellers, C2b a kind each
    "C1": (TALKER, {"kind": "scripted_talker", "params": {"price": 10.5, "offer_text": ACCEPT, "channel_id": "private_A_B"}}),
    "C2a": ({"kind": "constant", "params": {"price": 10.5}}, {"kind": "constant", "params": {"price": 11.0}}),
    "C2b": ({"kind": "constant", "params": {"price": 10.5}}, {"kind": "grim_trigger", "params": {"target": 11.0}}),
    "C3-accept": (TALKER, {"kind": "scripted_talker", "params": {"price": 10.5, "offer_text": ACCEPT, "channel_id": "private_A_B"}}),
    "C3-silent": (TALKER, {"kind": "constant", "params": {"price": 10.5}}),
}


def agents(condition: str) -> list[dict]:
    return [{"id": s, "role": "seller", "policy": p} for s, p in zip("AB", POLICIES[condition])]


def step(name: str, ok: bool, note: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {note}".rstrip(), flush=True)
    if not ok:
        FAILS.append(name)
    return ok


def tool(name: str, args: list[str], out: Path, env: dict[str, str]) -> str:
    """Run a consumer script on a clean environment; keep its output in OUT/<name>.txt, print it, fail on a non-zero exit.
    Returns its stdout."""
    p = subprocess.run(args, cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=600, env=env)
    text = f"$ {' '.join(args)}\nexit {p.returncode}\n--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}"
    (out / f"{name}.txt").write_text(text, encoding="utf-8")
    print(f"\n===== {name} (exit {p.returncode})\n{p.stdout[-4000:]}\n{p.stderr[-2500:]}", flush=True)
    step(f"{name} exit 0", p.returncode == 0, f"exit {p.returncode}")
    return p.stdout


def check_export(out: Path, n_rows: int) -> None:
    """The shape that analysis/p2_starter.py and figures/ read (DE-export-integration-report §2)."""
    data = json.loads((out / "features-B.json").read_text(encoding="utf-8"))
    rows, meta = data["rows"], data["meta"]
    cols = list(rows[0]) if rows else []
    want = (
        ID_COLUMNS
        + [c for f in sorted(FEATURES) for c in (f, f"{f}_missing")]
        + ["ablation_noop_rounds", "ablation_noop_share", "ablation_noop_share_all"]
    )
    step("export id columns in order", cols[: len(ID_COLUMNS)] == ID_COLUMNS, f"got {cols[: len(ID_COLUMNS)]}")
    step("export feature columns with _missing", set(cols) == set(want), f"extra={set(cols) - set(want)} missing={set(want) - set(cols)}")
    specs = meta.get("features") or []
    step("export meta keys", {"experiment_id", "layer", "features_code_hash", "features"} <= set(meta), f"got {sorted(meta)}")
    step("export feature specs", {s.get("id") for s in specs} == FEATURES and all(set(s) == SPEC_KEYS for s in specs),
         f"ids={sorted(s.get('id') for s in specs)}")  # fmt: skip
    step("export is not marked fake", not (meta.get("fake") or meta.get("FAKE_DATA")))
    labels = {(r["label_legal"], r["label_harm"]) for r in rows}
    step("export rows", len(rows) == n_rows, f"{len(rows)}/{n_rows}")
    step("labels yes/no", labels <= {(a, b) for a in ("yes", "no") for b in ("yes", "no")}, f"got {sorted(labels)}")
    subs = {(r["condition_id"], r["group"], r["sub_condition"]) for r in rows}
    want_subs = {(c, "C3" if c in SUB else c, SUB.get(c)) for c, _, _ in subs}
    step("group and sub_condition per condition", subs == want_subs, f"got {sorted(subs, key=str)}")
    header = (out / "features-B.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
    step("csv header equals the json columns", header == cols, f"csv has {len(header)} columns, json {len(cols)}")


def run(out: Path, env: dict[str, str], url: str, srv: subprocess.Popen, n_seeds: int) -> None:
    for _ in range(120):
        rc, body, _ = smoke.cli(env, "health", timeout=30)
        if rc == 0 and isinstance(body, dict) and body.get("status") == "ok":
            break
        if srv.poll() is not None:
            step("health", False, "server exited")
            return
        time.sleep(1)
    smoke.api(url, "/research/decision-rule", "PUT", {"text": DECISION_RULE})
    (out / "decision-rule.yaml").write_text(DECISION_RULE, encoding="utf-8")
    rc, _, err = smoke.cli(env, "freeze", "run", "--stage", "conditions", "--approved-by", smoke.APPROVER)
    if not step("freeze stage conditions (opens dev)", rc == 0, err if rc else ""):
        return
    _, seeds, _ = smoke.cli(env, "seeds", "list")
    conditions = sorted(c["id"] for c in smoke.api(url, "/conditions")[1])
    dev = seeds["dev"][:n_seeds]
    spec = smoke.experiment(conditions, "dev", dev, agents=agents("C1"), branch_plan=smoke.BRANCHES, name="export integration")
    spec |= {"agents_by_condition": {c: agents(c) for c in conditions if c in POLICIES}, "batch_budget": {"max_calls": 1000}}
    rc, exp, err = smoke.cli(env, "experiments", "start", "--file", smoke.write(out, "exp.json", spec), "--wait", "--max-wait", "1500",
                             timeout=1600)  # fmt: skip
    exp_id = exp.get("id") if isinstance(exp, dict) else None
    if not step("experiment (all conditions, dev seeds, probe + placebo + ablation)", rc == 0 and bool(exp_id),
                f"{exp_id} conditions={conditions} seeds={dev}" if exp_id else err):  # fmt: skip
        return
    cells = exp.get("cells") or []
    bad = [(c.get("condition_id"), c.get("seed"), c.get("status")) for c in cells if c.get("status") != "completed"]
    kids = [b for c in cells for b in c.get("branches") or []]
    step("every cell and child completed", len(cells) == len(conditions) * len(dev) and not bad and len(kids) == KIDS * len(cells)
         and all(b["status"] == "completed" for b in kids), f"cells={len(cells)} not completed={bad} children={len(kids)}")  # fmt: skip
    for layer in ("A", "B"):
        rc, _, err = smoke.cli(env, "features", "export", exp_id, "--layer", layer, "-o", str(out / f"features-{layer}.json"))
        step(f"features export json layer {layer}", rc == 0, err if rc else "")
    rc, summ, err = smoke.cli(env, "features", "export", exp_id, "--format", "csv", "-o", str(out / "features-B.csv"))
    meta = Path(summ["meta_path"]) if rc == 0 and isinstance(summ, dict) and summ.get("meta_path") else None
    step("features export csv layer B (+ .meta.json)", rc == 0 and bool(meta) and meta.exists(), err if rc else str(meta))
    if not (out / "features-B.json").exists() or not (out / "features-B.csv").exists():
        return
    check_export(out, len(cells))
    runs = out / "runs"
    runs.mkdir(exist_ok=True)
    rows = json.loads((out / "features-B.json").read_text(encoding="utf-8"))["rows"]
    for r in rows:
        smoke.cli(env, "runs", "export", r["parent_run_id"], "--format", "csv_rounds", "-o", str(runs / f"{r['parent_run_id']}.csv"))
    n_parents = sum((runs / f"{r['parent_run_id']}.csv").exists() for r in rows)
    step("csv_rounds per parent", n_parents == len(rows), f"{n_parents}/{len(rows)}")
    # fig4/4b read each probe child's rounds and its branch (fork_t, probe target) beside the parents' files
    kids_ok, child_ids = 0, [c for r in rows for c in (r["child_run_ids"] or "").split(";") if c]
    for cid in child_ids:
        rc1, _, _ = smoke.cli(env, "runs", "export", cid, "--format", "csv_rounds", "-o", str(runs / f"{cid}.csv"))
        rc2, detail, _ = smoke.cli(env, "runs", "show", cid)
        if rc2 == 0 and isinstance(detail, dict) and detail.get("branch"):
            (runs / f"{cid}.json").write_text(json.dumps(detail, indent=1), encoding="utf-8")
        kids_ok += rc1 == 0 and (runs / f"{cid}.csv").exists() and (runs / f"{cid}.json").exists()
    step("csv_rounds and run detail per child", kids_ok == len(child_ids) == KIDS * len(rows), f"{kids_ok}/{len(child_ids)}")
    # DECISIONS-13 §7: one job per block on the whole experiment; each takes the parents of its conditions, skips the rest
    blocks = {b["id"]: b for b in smoke.api(url, "/judge/blocks")[1]}
    ev_ids = []
    for block in ("main", "support_one_wording"):
        ev = {"experiment_id": exp_id, "block": block, "view": "talk_only", "item_set_id": "items-smoke", "backends": {"scripted": {}}}
        rc, job, err = smoke.cli(env, "evaluations", "start", "--file", smoke.write(out, f"ev-{block}.json", ev), "--wait",
                                 "--max-wait", "300", timeout=360)  # fmt: skip
        ev_id = job.get("id") if isinstance(job, dict) else None
        if not step(f"scripted judge job {block} completed", rc == 0 and bool(ev_id) and job.get("status") == "completed",
                    f"{ev_id} status={job.get('status')}" if ev_id else err):  # fmt: skip
            return
        ev_ids.append(ev_id)
        detail = smoke.api(url, f"/evaluations/{ev_id}")[1]
        (out / f"evaluation-{block}.json").write_text(json.dumps(detail, indent=1), encoding="utf-8")
        mine = sum(c.get("condition_id") in blocks[block]["conditions"] for c in cells)
        got, skipped = len(detail.get("episodes") or []), len(detail.get("skipped") or [])
        step(f"judge episodes {block} (its conditions; the others skipped)", got == mine and got + skipped == len(cells),
             f"{got} judged, {skipped} skipped of {len(cells)}")  # fmt: skip
    judged = out / "features-judged.json"
    args = [a for e in ev_ids for a in ("--evaluation", e)]
    rc, b, _ = smoke.cli(env, "features", "export", exp_id, *args, "-o", str(judged))
    # The judge columns come from an llm backend only (paid): a scripted job is refused with 422 path evaluation_id/0.
    paths = [e.get("path") for e in b.get("errors", [])] if isinstance(b, dict) else []
    step("features export with both evaluations (scripted jobs: 422 expected)",
         rc == 1 and smoke.code_of(b) == "validation_error" and "evaluation_id/0" in paths and not judged.exists(),
         f"exit {rc} code={smoke.code_of(b)} paths={paths}")  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="folder for every file the check writes (kept), outside the repo or git-ignored; default a new "
                    "temporary folder")  # fmt: skip
    ap.add_argument("--seeds", type=int, default=2, help="dev seeds per condition")
    a = ap.parse_args()
    out = (Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="export-integration-"))).resolve()
    # a path inside OUT: git matches a folder-only ignore rule (pilot-*/*-run/) only to an existing folder or its contents
    if out.is_relative_to(REPO) and subprocess.run(["git", "check-ignore", "-q", str(out / ".keep")], cwd=REPO).returncode != 0:
        print(f"--out {out} is in the repo and not git-ignored: it would hold exports and per-round data", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="export-root-"))
    root = tmp / "root"
    root.mkdir()
    for d in ("conditions", "scenarios"):
        shutil.copytree(REPO / d, root / d, ignore=shutil.ignore_patterns("frozen.json"))
    (root / "conditions" / "judge-rubric.yaml").write_text(json.dumps(smoke.RUBRIC), encoding="utf-8")
    smoke.write_models(root)
    port = smoke.free_port()
    url = f"http://127.0.0.1:{port}/api"
    log = open(out / "server.log", "wb")
    srv = subprocess.Popen([*smoke.CLI, "serve", "--root", str(root), "--port", str(port), "--no-durable"], cwd=REPO,
                           env=smoke.clean_env(root, None), stdout=log, stderr=log)  # fmt: skip
    try:
        run(out, smoke.clean_env(root, url), url, srv, a.seeds)
    except Exception as e:  # noqa: BLE001
        step("unexpected error", False, repr(e))
    finally:
        smoke.kill_tree(srv)
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)
    env = smoke.clean_env(root, None) | {"OPENBLAS_NUM_THREADS": "1"}  # the consumers need no secret; low memory (fe)
    if (out / "features-B.json").exists():
        report = ["--out", str(out / "p2-report-dev.md")]  # its default path is under analysis/
        rule = ["--decision-rule", str(out / "decision-rule.yaml")]
        tool("p2_starter-dev", [*P2, str(out / "features-B.json"), "--split", "dev", *rule, *report], out, env)
        for name, path in (("figures-json", "features-B.json"), ("figures-csv", "features-B.csv")):
            text = tool(name, [*FIGS, "--export", str(out / path), "--runs-dir", str(out / "runs"), "--out", str(out / name)], out, env)
            skipped = [x for x in text.splitlines() if x.startswith(("SKIP fig4_impulse", "SKIP fig4b_event_study"))]
            step(f"{name}: fig4 and fig4b drawn from the probe children", not skipped, "; ".join(skipped)[:300])
    else:
        step("feature export written", False)
    print(f"\nfiles in {out}")
    print(f"{len(FAILS)} failed: {FAILS}" if FAILS else "all steps passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
