"""Defect injection (CR-78 §3.5): one defect per invariant; each must make tests/test_callplan.py fail. Each defect is
put into the source, the test file runs, and the source is restored. Run from the repo root, with no other test run in the
same tree:

    uv run --frozen --offline python scripts/inject_callplan_defects.py . [name prefix, for example I11]
"""

import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
CP = "src/simcore/callplan.py"
MUTANTS = [
    ("I1 news off keeps get_news", CP, '({"get_news"} if not setting.news else set())', "set()"),
    ("I2 talk call without a postable channel", CP, '(("talk",) if postable else ())', '("talk",)'),
    (
        "I3 rival prices without the grant",
        CP,
        "{g for g, tool in GATES.items() if tool in granted}",
        "{g for g, tool in GATES.items() if tool in granted} | {'rival_prices'}",
    ),
    ("I4 pull pushes the record", CP, 'if mode == "pull":\n            record', 'if mode == "never":\n            record'),
    ("I5 a talk call in both offers every read", CP, '(mode == "pull" and t in TALK_READS)', '(mode != "push")'),
    ("I6 a 0 query limit still offers reads", CP, 'if mode != "push" and limit > 0 else ()', 'if mode != "push" else ()'),
    ("I7 a C3 act call reads the channel", CP, "sees = () if isolated else readable", "sees = readable"),
    (
        "I11 one seller's grant changes the other's plan",
        CP,
        "limit = setting.limits.queries(cap.max_queries_per_round)[0]",
        "limit = min(setting.limits.queries(c.max_queries_per_round)[0] for c in setting.capabilities.values())",
    ),
    (
        "I8 push skips the mask on the whole record",
        "src/simcore/view.py",
        "        for mask in state.masks:\n            if mask.channel_id == channel_id and t >= mask.from_t:",
        "        for mask in state.masks if limit else ():\n            if mask.channel_id == channel_id and t >= mask.from_t:",
    ),
    (
        "I9 the policy offers reads the plan does not",
        "src/adapters/llm/policy.py",
        "plan.offered if tools.queries_left",
        "plan.reads if tools.queries_left",
    ),
    (
        "I13 the estimate counts a talk turn per post grant",
        "src/services/estimate.py",
        "for purpose in plan.sellers[a.id].calls:\n                out[purpose] += 1",
        "for purpose in plan.sellers[a.id].calls:\n                out[purpose] += 1\n"
        '            out["talk"] += a.capability.grant("post") is not None and "talk" not in plan.sellers[a.id].calls',
    ),
]

for name, rel, old, new in [m for m in MUTANTS if not sys.argv[2:] or m[0].startswith(sys.argv[2])]:
    path = root / rel
    original = path.read_text(encoding="utf-8")
    assert original.count(old) == 1, name
    path.write_text(original.replace(old, new), encoding="utf-8", newline="\n")
    try:
        r = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "--offline",
                "python",
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-q",
                "-o",
                "addopts=",
                "-x",
                "tests/test_callplan.py",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=600,
        )
        failed = [ln for ln in r.stdout.splitlines() if ln.startswith("FAILED")]
        print(f"{name}: {'caught by ' + failed[0].split('::')[1].split(' ')[0] if failed else 'NOT CAUGHT'}", flush=True)
    finally:
        path.write_text(original, encoding="utf-8", newline="\n")
