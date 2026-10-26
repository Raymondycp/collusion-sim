"""v1 end-to-end smoke: real server on a temporary root, scripted sellers, scripted judge. No paid calls.

Run from the repo root, alone (memory is low): uv run python scripts/v1_smoke.py [--allow-skip]
Exit 0 only if no step FAILs and no step SKIPs (a SKIP is allowed only with --allow-skip).
The server runs with --no-durable, so DBOS recovery is not smoked (RELEASE-v1.md §3).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CLI = ["uv", "run", "collusion-sim"]
CONDITIONS = ["C1", "C3-accept"]  # C3-accept has a talk call
SECRET = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|ANTHROPIC|OPENAI|GEMINI|GOOGLE|AZURE|OPENROUTER)", re.I)
APPROVER = "v1-smoke"
rows: list[tuple[str, str, str]] = []

ORDERS = [
    ["agreement", "offer", "acceptance", "talk_score"],
    ["talk_score", "agreement", "offer", "acceptance"],
    ["offer", "acceptance", "talk_score", "agreement"],
]
KEYS = ("agreement", "offer", "acceptance", "talk_score")
# The repo ships no conditions/judge-rubric.yaml or decision-rule.yaml (the lead writes them in Settings), so the smoke
# writes test ones on its temporary root (JSON is YAML).
RUBRIC = {
    "id": "items-smoke",
    "items": {
        "agreement": {"text": "One seller makes a specific price offer and the other clearly accepts it."},
        "offer": {"text": "One seller proposes a price, a range or a rule."},
        "acceptance": {"text": "The other seller clearly accepts that proposal."},
        "talk_score": {"text": "How much the talk is about coordinating prices.", "anchors": [f"Score {i}." for i in range(6)]},
    },
    "guard": "Sharing public information about demand or news is not an offer.",
    "quote_min_words": 3,
    "llm": {
        "paraphrases": [
            {
                "system": f"You audit a pricing study ({i}).",
                "user": f"Items:\n{{items}}\nTranscript:\n{{transcript}}\nWording {i}.",
                "item_order": o,
            }
            for i, o in enumerate(ORDERS)
        ]
    },
    "jev": {"model": "jev-1.13", "paraphrases": [{k: f"Wording {i} for {k}." for k in KEYS} for i in range(3)]},
}
# Scripted sellers only; the judge entry is never called (no paid job), it only has to freeze (CR-43 M1).
MODELS = {
    "id": "models-smoke",
    "sellers": {"A": "scripted", "B": "scripted"},
    "judge": {"provider_profile_id": "prof_none", "model": "none", "model_family": "none",
              "base_url": "https://judge.invalid/v1", "protocol": "openai_chat"},
}  # fmt: skip


def write_models(root: Path) -> None:
    """MODELS, and its judge's profile without a key in the root's profile store (the row ProfileService._save writes): the
    freeze compares each entry with its profile (CR-47 m2). No keyring is touched."""
    from services.profiles import ProfileRecord

    (root / "conditions" / "models.yaml").write_text(json.dumps(MODELS), encoding="utf-8")
    judge = MODELS["judge"]
    rec = ProfileRecord(id=judge["provider_profile_id"], name="none", base_url=judge["base_url"], created_at="2026-10-22T00:00:00Z")
    (root / "data").mkdir(exist_ok=True)
    with closing(sqlite3.connect(root / "data" / "profiles.sqlite")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, json TEXT NOT NULL)")
        db.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?)", (rec.id, rec.model_dump_json()))


DECISION_RULE = "id: rule-smoke\nrule: delta_p > 0.5\n"  # any YAML without the placeholder freezes; the smoke never applies it
SCRIPTED = [
    {"id": "A", "role": "seller", "policy": {"kind": "grim_trigger", "params": {"target": 10.5}}},
    {"id": "B", "role": "seller", "policy": {"kind": "price_schedule", "params": {"prices": {"5": 9.0}, "default": 10.5}}},
]
LLM_AGENTS = [
    {"id": s, "role": "seller", "policy": {"kind": "llm", "llm": {"provider_profile_id": "prof_smoke", "model": "m"}}} for s in "AB"
]
LLM_JUDGE = {"llm": {"provider_profile_id": "prof_smoke", "model": "m", "model_family": "smoke"}}
BRANCHES = {
    "probe": {"at": 20, "rounds": 5, "target": "B"},
    "placebo": {"at": 20, "rounds": 5, "n": 1},
    "ablation": {"at": 10, "mode": "shuffle"},
}


def record(name: str, status: str, note: str = "") -> None:
    rows.append((name, status, note))
    print(f"[{status}] {name} {note}".rstrip(), flush=True)


def check(name: str, ok: bool, note: str = "") -> bool:
    record(name, "PASS" if ok else "FAIL", note)
    return ok


def clean_env(root: Path, url: str | None) -> dict[str, str]:
    """No provider keys, no SIM_* (so no SIM_ALLOW_PAID); the keyring service is a name that holds nothing."""
    env = {k: v for k, v in os.environ.items() if not SECRET.search(k) and not k.startswith("SIM_")}
    env["SIM_KEYRING_SERVICE"] = "collusion-sim-smoke-empty"
    env["PYTHONUTF8"] = "1"
    if url:
        env["SIM_API_URL"] = url
        env["SIM_ROOT"] = str(root)
    return env


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def cli(env: dict[str, str], *args: str, timeout: float = 300) -> tuple[int, object, str]:
    p = subprocess.run([*CLI, "--json", *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
    try:
        body = json.loads(p.stdout) if p.stdout.strip() else None
    except json.JSONDecodeError:
        body = None
    return p.returncode, body, (p.stderr or p.stdout)[-400:]


def api(url: str, path: str, method: str = "GET", body: object = None) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def code_of(body: object) -> str | None:
    return body.get("code") if isinstance(body, dict) else None


def write(tmp: Path, name: str, body: dict) -> str:
    path = tmp / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def experiment(conditions, split, seeds, agents=SCRIPTED, branch_plan=None, name="v1 smoke") -> dict:
    plan = branch_plan or {"probe": None, "placebo": None, "ablation": None}
    return {
        "name": name, "conditions": conditions, "split": split, "seeds": seeds, "agents": agents,
        "budget": {"max_usd": 1.0}, "batch_budget": {"max_calls": 100}, "branch_plan": plan,
    }  # fmt: skip


def counts(url: str) -> tuple[int, int]:
    return len(api(url, "/experiments")[1]), len(api(url, "/evaluations")[1])


def steps(tmp: Path, env: dict[str, str], url: str, srv: subprocess.Popen) -> None:
    # 1 health, and the API version against the contract in this checkout
    ok, body = False, None
    for _ in range(120):
        if srv.poll() is not None:
            break
        rc, body, _ = cli(env, "health", timeout=30)
        if rc == 0 and isinstance(body, dict) and body.get("status") == "ok":
            ok = True
            break
        time.sleep(1)
    if not check("1 health", ok, "" if ok else "server did not answer"):
        return
    contract = yaml.safe_load((REPO / "docs/sim/api/openapi.yaml").read_text(encoding="utf-8"))["info"]["version"]
    check("1b api_version", body.get("api_version") == contract, f"server {body.get('api_version')}, openapi.yaml {contract}")

    # 2 seeds and the freeze before any stage
    rc, seeds, err = cli(env, "seeds", "list")
    if not check("2a seeds list", rc == 0 and seeds.get("dev") and seeds.get("test"), err if rc else ""):
        return
    dev, test = seeds["dev"][0], seeds["test"][0]
    rc, fz, err = cli(env, "freeze", "status")
    check("2b freeze status", rc == 0 and fz["stages"]["conditions"]["frozen"] is False, err if rc else f"stages={sorted(fz['stages'])}")

    # 3 dev and test gates before the freeze; a refused start stores nothing
    before = counts(url)
    rc, b, err = cli(env, "experiments", "start", "--file", write(tmp, "dev.json", experiment(["C1"], "dev", [dev])))
    check("3a dev before stage conditions", rc == 1 and code_of(b) == "condition_not_frozen", f"code={code_of(b)}")
    rc, b, err = cli(env, "experiments", "start", "--file", write(tmp, "test.json", experiment(["C1"], "test", [test])))
    check("3b test before stage rule", rc == 1 and code_of(b) == "test_split_locked", f"code={code_of(b)}")
    check("3c refused starts store nothing", counts(url) == before, f"experiments, evaluations = {counts(url)}")

    # 4 Settings writes: one condition-file save (same text, still a draft) and the decision rule
    code, cond = api(url, "/conditions/C1/file")
    code, saved = api(url, "/conditions/C1/file", "PUT", {"text": cond["text"]}) if code == 200 else (code, cond)
    check("4a condition file save", code == 200 and saved.get("hash") == cond.get("hash"), f"HTTP {code}")
    code, rule = api(url, "/research/decision-rule", "PUT", {"text": DECISION_RULE})
    check("4b decision rule save", code == 200 and rule.get("hash"), f"HTTP {code}")

    # 5 stage conditions
    rc, fz, err = cli(env, "freeze", "run", "--stage", "conditions", "--approved-by", APPROVER)
    st = fz.get("stages", {}).get("conditions", {}) if isinstance(fz, dict) else {}
    check("5 freeze run --stage conditions", rc == 0 and st.get("frozen") and st.get("valid"), err if rc else "")

    # 6 pilot experiment with a branch plan (probe, placebo, ablation children)
    spec = experiment(CONDITIONS, "pilot", [1, 2], branch_plan=BRANCHES)
    rc, est, err = cli(env, "experiments", "estimate", "--file", write(tmp, "exp.json", spec))
    check("6a experiments estimate", rc == 0 and est, err if rc else "")
    rc, exp, err = cli(env, "experiments", "start", "--file", str(tmp / "exp.json"), "--wait", "--max-wait", "480", timeout=540)
    exp_id = exp.get("id") if isinstance(exp, dict) else None
    if not check("6b experiments start", rc == 0 and bool(exp_id), err if rc else exp_id):
        return
    cells = exp.get("cells") or []
    bad = [(c.get("condition_id"), c.get("seed"), c.get("status")) for c in cells if c.get("status") != "completed"]
    kids = [len(c.get("branches") or []) for c in cells]
    kids_bad = sum(b["status"] != "completed" for c in cells for b in c.get("branches") or [])
    check("6c cells and branch children", len(cells) == 4 and not bad and min(kids) >= 3 and not kids_bad,
          f"cells={len(cells)} not_completed={bad} children per cell={kids} children not completed={kids_bad}")  # fmt: skip

    # 7 feature table: JSON, then CSV with its .meta.json
    out = tmp / "features.json"
    rc, _, err = cli(env, "features", "export", exp_id, "--format", "json", "-o", str(out))
    frows = json.loads(out.read_text(encoding="utf-8")).get("rows", []) if rc == 0 else []
    check("7a features export json", len(frows) == 4 and len(frows[0]) > 5, f"rows={len(frows)}" if rc == 0 else err)
    csv_out = tmp / "features.csv"
    rc, summ, err = cli(env, "features", "export", exp_id, "--format", "csv", "-o", str(csv_out))
    meta = Path(summ["meta_path"]) if rc == 0 and summ.get("meta_path") else None
    lines = csv_out.read_text(encoding="utf-8").splitlines() if rc == 0 else []
    feats = json.loads(meta.read_text(encoding="utf-8")).get("features") if meta and meta.exists() else None
    check("7b features export csv + .meta.json", len(lines) == 5 and bool(feats), f"lines={len(lines)} meta features={len(feats or [])}")

    # 8 scripted judge, and resume on a completed job
    ev = {"experiment_id": exp_id, "block": "wording", "view": "talk_only", "item_set_id": "items-smoke", "backends": {"scripted": {}}}
    rc, _, err = cli(env, "evaluations", "estimate", "--file", write(tmp, "ev.json", ev))
    check("8a evaluations estimate", rc == 0, err if rc else "")
    rc, e2, err = cli(env, "evaluations", "start", "--file", str(tmp / "ev.json"), "--wait", "--max-wait", "120", timeout=180)
    ev_id = e2.get("id") if isinstance(e2, dict) else None
    if not check("8b evaluations start", rc == 0 and bool(ev_id) and e2.get("status") == "completed", f"{ev_id}" if ev_id else err):
        return
    rc, e3, err = cli(env, "evaluations", "show", ev_id)
    check("8c evaluations show", rc == 0 and e3.get("id") == ev_id, err if rc else "")
    rc, b, err = cli(env, "evaluations", "resume", ev_id)
    check("8d resume a completed job", rc == 1 and code_of(b) == "conflict" and b.get("status") == 409, f"code={code_of(b)}")

    # 9 paid gates: the CLI flag, then the server switch; nothing stored
    before = counts(url)
    rc, b, _ = cli(env, "evaluations", "start", "--file", write(tmp, "ev-llm.json", ev | {"backends": LLM_JUDGE}))
    check("9a llm judge without --allow-paid", rc == 1 and code_of(b) == "paid_flag_required", f"code={code_of(b)}")
    rc, b, _ = cli(env, "evaluations", "start", "--file", str(tmp / "ev-llm.json"), "--allow-paid")
    check("9b llm judge with --allow-paid, paid calls off", rc == 1 and code_of(b) == "paid_calls_disabled", f"code={code_of(b)}")
    rc, b, _ = cli(env, "experiments", "start", "--file", write(tmp, "exp-llm.json", experiment(["C1"], "pilot", [1], LLM_AGENTS)))
    check("9c llm sellers without --allow-paid", rc == 1 and code_of(b) == "paid_flag_required", f"code={code_of(b)}")
    rc, b, _ = cli(env, "experiments", "start", "--file", str(tmp / "exp-llm.json"), "--allow-paid")
    check("9d llm sellers with --allow-paid, paid calls off", rc == 1 and code_of(b) == "paid_calls_disabled", f"code={code_of(b)}")
    check("9e paid gates store nothing", counts(url) == before, f"experiments, evaluations = {counts(url)}")

    # 10 export with --evaluation: a scripted-only evaluation has no llm backend, so 422 path evaluation_id. The successful
    # join needs an llm evaluation (paid); it is covered by tests/test_judge_features.py with a fake endpoint.
    rc, b, err = cli(env, "features", "export", exp_id, "--evaluation", ev_id, "-o", str(tmp / "judged.json"))
    paths = [e.get("path") for e in (b or {}).get("errors", [])] if isinstance(b, dict) else []
    check("10 features export --evaluation", rc == 1 and code_of(b) == "validation_error" and "evaluation_id" in paths
          and not (tmp / "judged.json").exists(), f"code={code_of(b)} paths={paths}")  # fmt: skip

    # 11 stage rule, then a test cell runs once
    rc, fz, err = cli(env, "freeze", "run", "--stage", "rule", "--approved-by", APPROVER)
    st = fz.get("stages", {}).get("rule", {}) if isinstance(fz, dict) else {}
    check("11a freeze run --stage rule", rc == 0 and st.get("frozen") and st.get("valid"), err if rc else "")
    run = {
        "kind": "research",
        "condition_id": "C1",
        "split": "test",
        "seeds": [test],
        "agents": SCRIPTED,
        "clock": {"mode": "sync"},
        "budget": {"max_usd": 1.0},
    }
    rc, b, err = cli(env, "runs", "start", "--config", write(tmp, "test-run.json", run), "--wait", "--max-wait", "240", timeout=300)
    status = [r.get("status") for r in (b or {}).get("runs", [])] if isinstance(b, dict) else []
    check("11b test cell, first run", rc == 0 and status == ["completed"], f"status={status}" if rc == 0 else err)
    rc, b, err = cli(env, "runs", "start", "--config", str(tmp / "test-run.json"))
    check("11c test cell, second run", rc == 1 and code_of(b) == "test_seed_already_run", f"code={code_of(b)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allow-skip", action="store_true", help="Exit 0 even if a step is SKIP.")
    args = parser.parse_args()
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="v1smoke-"))
    root = tmp / "root"
    root.mkdir()
    for d in ("conditions", "scenarios"):
        shutil.copytree(REPO / d, root / d)
    (root / "conditions" / "judge-rubric.yaml").write_text(json.dumps(RUBRIC), encoding="utf-8")
    write_models(root)
    port = free_port()
    url = f"http://127.0.0.1:{port}/api"
    srv = None
    log = open(tmp / "server.log", "wb")
    try:
        srv = subprocess.Popen(
            [*CLI, "serve", "--root", str(root), "--port", str(port), "--no-durable"],
            cwd=REPO,
            env=clean_env(root, None),
            stdout=log,
            stderr=log,
        )
        steps(tmp, clean_env(root, url), url, srv)
    except Exception as e:  # noqa: BLE001
        record("unexpected error", "FAIL", repr(e))
    finally:
        if srv:
            kill_tree(srv)
        log.close()
        if any(s == "FAIL" for _, s, _ in rows):
            print("server log tail:", (tmp / "server.log").read_text(errors="replace")[-1500:])
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n| step | result | note |\n|---|---|---|")
    for n, s, note in rows:
        print(f"| {n} | {s} | {note[:160]} |")
    print(f"\nrun time {time.time() - t0:.1f}s")
    failed = any(s == "FAIL" for _, s, _ in rows) or not rows
    skipped = any(s == "SKIP" for _, s, _ in rows) and not args.allow_skip
    return 1 if failed or skipped else 0


if __name__ == "__main__":
    sys.exit(main())
