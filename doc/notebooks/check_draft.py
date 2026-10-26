"""Check that no pilot number survives in the elastic paper draft (DECISIONS-36 §7).

    python check_draft.py [draft.md]

FLAG lines: the tag [PILOT - REPLACE], a path into out/dev/ (the pilot outputs), the pilot experiment id, or a pilot run id
(read from paper/data/haiku-elastic/dev/experiment.zip). Exit 1 if any FLAG. The notebook's Part D runs the same check.
"""
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRAFT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "paper" / "drafts-elastic" / "paper-elastic.md"
PILOT_EXP = "exp_01M5QY9834PPBPRQDANQGHTS9R"
TAG = "[PILOT - REPLACE]"


def pilot_run_ids():
    z = ROOT / "paper" / "data" / "haiku-elastic" / "dev" / "experiment.zip"
    if not z.is_file():
        return set()
    return {n.split("/")[1] for n in zipfile.ZipFile(z).namelist() if n.startswith("runs/") and n.count("/") == 2}


def check(text):
    lines = text.splitlines()
    runs = pilot_run_ids()
    found = {
        TAG: [i for i, l in enumerate(lines, 1) if TAG in l],
        "out/dev/ (pilot outputs)": [i for i, l in enumerate(lines, 1) if re.search(r"out/dev/", l)],
        PILOT_EXP: [i for i, l in enumerate(lines, 1) if PILOT_EXP in l],
        "pilot run ids": [i for i, l in enumerate(lines, 1) if any(r in l for r in runs)],
    }
    return found, len(runs)


if __name__ == "__main__":
    found, n_runs = check(DRAFT.read_text(encoding="utf-8"))
    bad = False
    for what, at in found.items():
        print(f"{'FLAG' if at else 'PASS'}  draft has no {what}" + (f"  ({len(at)} lines, first {at[:10]})" if at else ""))
        bad |= bool(at)
    shown = DRAFT.resolve().relative_to(ROOT).as_posix() if DRAFT.resolve().is_relative_to(ROOT) else DRAFT.name   # no user path in the output
    print(f"checked {shown} against {n_runs} pilot run ids")
    sys.exit(1 if bad else 0)
