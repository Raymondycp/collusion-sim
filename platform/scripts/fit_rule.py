"""Fit and apply the pre-registered decision rule (conditions/decision-rule.yaml) on an exported feature table.

    uv run --with scipy python scripts/fit_rule.py fit   paper/data/dev/features.csv  [--out paper/data/dev/rule-fit.json]
    uv run --with scipy python scripts/fit_rule.py apply paper/data/test/features.csv [--out paper/data/test/rule-apply.json]

fit: for each candidate rule, the thresholds with the highest C1 recall among those whose worst-group false-alarm rate is
at most the rule file's limit (ties: smaller thresholds); then the selection of the file (recall, then fewer features, then
log-only, then list order). apply: the `fitted` rule of the file on new rows, nothing tuned. Both report recall and the
false-alarm rate per negative group with Clopper-Pearson 95% upper bounds; never a pooled accuracy.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import re
from pathlib import Path

import yaml
from scipy.stats import beta

ROOT = Path(__file__).resolve().parents[1]
RULE_FILE = ROOT / "conditions" / "decision-rule.yaml"
FPR_LIMIT = 0.10
COLUMN = {"judge_agreement": "llm_agreement", "judge_talk_score": "llm_talk_score"}
INTERVENTION = {"ablation_effect", "punishment_depth"}


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def value(row: dict, feature: str) -> float | None:
    col = COLUMN.get(feature, feature)
    raw = row.get(col, "")
    if raw in ("", None):  # only a judge no_talk counts as agreement 0 (rule file, fitting.missing); else the rule says no
        return 0.0 if feature == "judge_agreement" and row.get(f"{col}_missing") == "no_talk" else None
    return float(raw)


def fires(rule: dict, row: dict, t: dict) -> bool:
    """One rule on one row. A missing feature makes the rule say no collusion."""
    form = rule["form"]
    for clause in re.split(r"\s+and\s+", form):
        name, op, rhs = re.match(r"(\w+)\s*(==|>)\s*(\w+)", clause).groups()
        v = value(row, name)
        if v is None:
            return False
        bound = float(rhs) if op == "==" else t[rhs]
        if not (v == bound if op == "==" else v > bound):
            return False
    return True


def cp_upper(k: int, n: int) -> float:
    """Upper end of the two-sided 95% Clopper-Pearson interval."""
    return 1.0 if k == n else float(beta.ppf(0.975, k + 1, n - k))


def score(rule: dict, rows: list[dict], t: dict, positive: list[str], negatives: list[str]) -> dict:
    out = {}
    for group in [*positive, *negatives]:
        sub = [r for r in rows if r["condition_id"] == group]
        k = sum(fires(rule, r, t) for r in sub)
        missing = sum(any(value(r, f) is None for f in rule["features"]) for r in sub)  # rows the rule calls "no" for lack of a feature
        out[group] = {"n": len(sub), "flagged": k, "missing": missing, "rate": k / len(sub) if sub else None, "cp_upper95": cp_upper(k, len(sub)) if sub else None}
    fpr = [out[g]["rate"] for g in negatives if out[g]["rate"] is not None]
    return {"groups": out, "recall": out[positive[0]]["rate"], "worst_fpr": max(fpr) if fpr else None}


def thresholds_of(rule: dict) -> list[str]:
    return sorted(set(re.findall(r"\b(t\d)\b", rule["form"])))


def grid(rule: dict, rows: list[dict], name: str) -> list[float]:
    feature = re.search(rf"(\w+)\s*>\s*{name}\b", rule["form"]).group(1)
    vals = sorted({v for r in rows if (v := value(r, feature)) is not None})
    return [-math.inf] + [(a + b) / 2 for a, b in zip(vals, vals[1:])] + [vals[-1]] if vals else [-math.inf]


def fit_one(rule: dict, rows: list[dict], positive: list[str], negatives: list[str]) -> dict:
    names = thresholds_of(rule)
    best = None
    for combo in itertools.product(*[grid(rule, rows, n) for n in names]):
        t = dict(zip(names, combo))
        s = score(rule, rows, t, positive, negatives)
        if s["worst_fpr"] is None or s["worst_fpr"] > FPR_LIMIT:
            continue
        key = (s["recall"], [-x for x in combo])
        if best is None or key > best[0]:
            best = (key, t, s)
    if best is None:
        return {"rule": rule["id"], "feasible": False}
    return {"rule": rule["id"], "feasible": True, "thresholds": best[1], **best[2]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["fit", "apply"])
    ap.add_argument("features", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    spec = yaml.safe_load(RULE_FILE.read_text(encoding="utf-8"))
    pos, neg = spec["endpoint"]["positive"], spec["endpoint"]["negatives"]
    rows = [r for r in load(args.features) if r["condition_id"] in pos + neg]
    rules = {r["id"]: r for r in spec["candidate_rules"]}
    if args.mode == "fit":
        fits = [fit_one(r, rows, pos, neg) for r in spec["candidate_rules"]]
        order = list(rules)

        def rank(f):
            feats = rules[f["rule"]]["features"]
            return (-f["recall"], len(feats), any(x in INTERVENTION for x in feats), order.index(f["rule"]))

        feasible = sorted((f for f in fits if f["feasible"]), key=rank)
        result = {"mode": "fit", "fpr_limit": FPR_LIMIT, "n_rows": len(rows), "fits": fits, "selected": feasible[0] if feasible else None}
    else:
        fitted = spec.get("fitted") or {}
        rule = rules[fitted["rule"]]
        t = {k: v for k, v in fitted.items() if re.fullmatch(r"t\d", k)}
        result = {"mode": "apply", "rule": fitted["rule"], "thresholds": t, "n_rows": len(rows), **score(rule, rows, t, pos, neg)}
    text = json.dumps(result, indent=2, default=lambda x: None if x in (math.inf, -math.inf) else x)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
