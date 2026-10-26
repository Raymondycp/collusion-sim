"""Rerun the notebooks of this package and compare the result with processed-data/.

The builders were written for the project repository layout. This script copies the package files into that layout in a
work folder, runs the notebooks there, and compares every table it writes with the copy in processed-data/.
No paid call: the judge scores come from raw-data/judge-cache/.

Usage, from the package root:
    python doc/notebooks/rerun.py <python with the packages of doc/README.md> [work folder] [dev2|test|all]
"""
import filecmp
import os
import shutil
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[2]
PY = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
WORK = Path(sys.argv[2] if len(sys.argv) > 2 else PKG / "rerun-work").resolve()
SETS = ["dev2", "test"] if len(sys.argv) <= 3 or sys.argv[3] == "all" else [sys.argv[3]]
LAYOUT = {   # package path -> path in the repository layout
    "platform/pyproject.toml": "pyproject.toml",
    "platform/studies/haiku-elastic": "studies/haiku-elastic",
    "raw-data/exports/dev2": "paper/data/haiku-elastic/dev2",
    "raw-data/exports/dev2-pcplus": "paper/data/haiku-elastic/dev2-pcplus",
    "raw-data/exports/test": "paper/data/haiku-elastic/test",
    "raw-data/judge-cache/dev2": "paper/analysis-elastic/judge-cache-dev2",
    "raw-data/judge-cache/test": "paper/analysis-elastic/judge-cache-test",
    "doc/paper/paper-elastic.md": "paper/drafts-elastic/paper-elastic.md",
}
SCRIPTS = ["build_notebook.py", "build_example_notebook.py", "build_overview_notebook.py", "run_dev.sh", "dump_outputs.py", "check_draft.py"]

for src, dst in LAYOUT.items():
    s, d = PKG / src, WORK / dst
    d.parent.mkdir(parents=True, exist_ok=True)
    if s.is_dir():
        shutil.copytree(s, d, dirs_exist_ok=True)
    else:
        shutil.copy2(s, d)
for n in SCRIPTS:
    shutil.copy2(PKG / "doc/notebooks" / n, WORK / "paper/analysis-elastic" / n)

env = dict(os.environ, ANALYSIS_PY=PY, PYTHONIOENCODING="utf-8")
env.pop("JUDGE_ALLOW_PAID", None)   # never a paid call from a rerun
failed = []
for ds in SETS:
    print(f"== {ds}: run_dev.sh (5 to 10 minutes)", flush=True)
    r = subprocess.run(["bash", "paper/analysis-elastic/run_dev.sh"], cwd=WORK, env=dict(env, ANALYSIS_DATASET=ds))
    if r.returncode:
        failed.append(f"{ds}: run_dev.sh exit {r.returncode}")
        continue
    new = WORK / "paper/analysis-elastic/out" / ds / "tables"
    for f in sorted((PKG / "processed-data" / ds).glob("*.csv")):
        if not (new / f.name).is_file() or not filecmp.cmp(f, new / f.name, shallow=False):
            failed.append(f"{ds}: {f.name} differs from processed-data")
    print(f"{ds}: {len(list((PKG / 'processed-data' / ds).glob('*.csv')))} tables compared", flush=True)
print("work folder:", WORK)
print("RESULT:", "every table equal to processed-data" if not failed else "\n  ".join(["differences:", *failed]))
sys.exit(1 if failed else 0)
