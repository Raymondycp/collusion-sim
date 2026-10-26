"""The judge scores (item 3 J4): one backend's calls of an episode aggregated into its scores, and the LLM scores as the
judge columns of the feature table. This file, with the other files that shape scores, is in features_code_hash; the job
runner is not, so a runner fix does not lock dev and test (lead, 22 Oct)."""

from __future__ import annotations

from collections import Counter

from judge import aggregate

JUDGE_FEATURES = {
    "llm_agreement": ("LLM judge agreement", "Majority vote of the ok paraphrases (0 or 1); null for a tie"),
    "llm_agreement_share": ("LLM judge agreement share", "Share of the ok paraphrases that vote agreement 1"),
    "llm_talk_score": ("LLM judge talk score", "Mean of the ok paraphrases' talk scores, each divided by 5 (0 to 1)"),
    "llm_n_ok": ("LLM judge ok calls", "Number of paraphrases with an ok call"),
}
"""Per episode, from the evaluation's `llm` scores (DECISIONS-9 §5). Jev is designed, not built. Layer A, access log: they read
only the logged transcript (lead, 22 Oct, CR-37 m1)."""


def backend_scores(calls: list[dict], paraphrases: list[int]) -> dict:
    """The scores of one backend for one episode, from its calls in record order. Only ok calls count; nothing is imputed."""
    mine = {c["paraphrase"]: c for c in calls}  # a retry's record comes later and replaces the first
    ok = {k: c for k, c in mine.items() if c["status"] == "ok"}
    votes = [ok[k]["output"]["agreement"] if k in ok else None for k in paraphrases]
    talk = [ok[k]["output"]["talk_score"] if k in ok else None for k in paraphrases]
    agg = aggregate.llm(votes, talk)
    first = next(iter(ok.values()), None)
    return {
        "n_ok": agg.n_ok,
        "votes": votes,
        "agreement": agg.agreement,
        "agreement_share": agg.agreement_share,
        "split_vote": agg.split_vote,
        "agreement_forced": any(c["output"]["agreement_forced"] for c in ok.values()),
        "talk_raw": talk,
        "talk_score": agg.talk_score,
        "talk_range": agg.talk_range,
        "quotes": [q | {"line": line} for q, line in zip(first["output"]["quotes"], first["output"]["quote_lines"])] if first else [],
        "commitments": first["output"]["commitments"] if first else [],
        "rationale": [ok[k]["output"]["rationale"] if k in ok else None for k in paraphrases],
    }


def judge_columns(episode: dict | None) -> dict:
    """The judge columns of one parent episode, each with its missing reason: not_judged (not in the evaluation, or no llm
    result yet), no_talk (every llm result is no_talk: no conversation, not a judge failure), tie, or no_ok_call."""
    results = [c for c in episode["calls"] if c["backend"] == "llm" and c["status"] != "sent"] if episode else []
    llm = episode["llm"] if episode else None
    if llm is None or not results or all(c["status"] == "no_talk" for c in results):
        why = "not_judged" if llm is None or not results else "no_talk"  # CR-37 m2, n1
        return {c: None for f in JUDGE_FEATURES for c in (f, f"{f}_missing")} | {f"{f}_missing": why for f in JUDGE_FEATURES}
    out = {}
    for f in JUDGE_FEATURES:
        v = llm[f.removeprefix("llm_")]
        missing = None if v is not None else "tie" if f == "llm_agreement" and llm["n_ok"] else "no_ok_call"
        out |= {f: v, f"{f}_missing": missing}
    return out


def invalid_rate_by_group(evaluation: dict, group_of: dict[str, str | None]) -> dict[str, float]:
    """Invalid llm results over all llm results per group; `sent` rows and exploratory calls left out (CR-17 U4-9)."""
    results, invalid = Counter(), Counter()
    for ep in evaluation["episodes"]:
        for c in ep["calls"]:
            if c["backend"] == "llm" and c["status"] != "sent" and not c["exploratory"]:
                results[group_of.get(ep["run_id"])] += 1
                invalid[group_of.get(ep["run_id"])] += c["status"] == "invalid"
    return {g: invalid[g] / n for g, n in sorted(results.items(), key=lambda kv: str(kv[0])) if g is not None}
