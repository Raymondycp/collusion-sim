"""Write runs-check.csv next to an experiment.zip: one row per parent run, counted from its episode.jsonl events.
The notebook compares these counts with meta.json and with its own event parsing (lead check, one file per export folder).
Usage: python make_runs_check.py <folder with experiment.zip> [...]"""
import csv
import json
import sys
import zipfile
from pathlib import Path

COLS = ["condition", "seed", "run_id", "end", "rounds", "llm_calls", "call_not_ok", "acted", "fallback", "posts_private", "posts_public"]

for folder in map(Path, sys.argv[1:]):
    z = zipfile.ZipFile(folder / "experiment.zip")
    rows = []
    for name in sorted(n for n in z.namelist() if n.endswith("/episode.jsonl")):
        meta = json.loads(z.read(name.replace("episode.jsonl", "meta.json")))
        r = dict(condition=meta["condition_id"], seed=meta["seed"], run_id=meta["id"], end=meta["end_reason"], rounds=meta["t"],
                 llm_calls=0, call_not_ok=0, acted=0, fallback=0, posts_private=0, posts_public=0)
        ok_by_call = {}   # call_not_ok: calls with no successful attempt (a failed try that a later attempt of the same agent, round and purpose fixed does not count; a retry has a new request hash)
        for line in z.open(name):
            e = json.loads(line)
            t = e.get("type")
            if t == "llm_call":
                r["llm_calls"] += 1
                key = (e["agent_id"], e["t"], e["purpose"])
                ok_by_call[key] = ok_by_call.get(key, False) or e.get("error") is None
            elif t == "acted":
                r["acted"] += 1
                r["fallback"] += bool(e.get("fallback") or e.get("forced"))
            elif t == "posted":
                r["posts_private" if e["channel_id"].startswith("private") else "posts_public"] += 1
        r["call_not_ok"] = sum(not ok for ok in ok_by_call.values())
        rows.append(r)
    rows.sort(key=lambda r: (r["condition"], r["seed"]))
    with open(folder / "runs-check.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, COLS)
        w.writeheader()
        w.writerows(rows)
    print(folder, len(rows), "runs")
