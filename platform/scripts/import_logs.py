"""Import outside conversation logs as an episode.v5 and its trail.jsonl (QUEUE "Log import"; DECISIONS-14 §3.3). No LLM call.

    uv run python scripts/import_logs.py pi A=SESSION_A.jsonl B=SESSION_B.jsonl -o OUT [--features FEATURES.csv]
    uv run python scripts/import_logs.py rows LOG.json|LOG.csv -o OUT [--created-at ISO] [--features FEATURES.csv]

Writes OUT/<run_id>/episode.jsonl and OUT/<run_id>/trail.jsonl, and prints the run id with the event counts. --features
writes one CSV row of every feature: the log features on the tier-A events, the others missing with reason
access_model or access_intervention. The input formats are in adapters/log_import.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from adapters.log_import import Imported, LogImportError, feature_row, import_pi, import_rows
from features.table import to_csv


def write(imported: Imported, out: Path) -> Path:
    folder = out / imported.run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "episode.jsonl").write_bytes(imported.episode_text().encode("utf-8"))
    (folder / "trail.jsonl").write_bytes(imported.trail_text().encode("utf-8"))
    return folder


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("format", choices=["pi", "rows"])
    ap.add_argument("inputs", nargs="+", help="pi: AGENT=SESSION.jsonl per seller; rows: one JSON or CSV file")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--created-at", default="1970-01-01T00:00:00Z", help="rows only: the episode's created_at")
    ap.add_argument("--features", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.format == "pi":
            pairs = [i.split("=", 1) for i in args.inputs]
            if any(len(p) != 2 for p in pairs):
                raise LogImportError("pi inputs are AGENT=SESSION.jsonl")
            imported = import_pi({a: Path(p).read_bytes() for a, p in pairs})
        else:
            if len(args.inputs) != 1:
                raise LogImportError("rows takes one file")
            imported = import_rows(Path(args.inputs[0]).name, Path(args.inputs[0]).read_bytes(), args.created_at)
    except (LogImportError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    folder = write(imported, args.out)
    if args.features:
        row = feature_row(imported)
        args.features.write_text(to_csv(list(row), [row]), encoding="utf-8")
    events = Counter(json.loads(line)["type"] for line in imported.lines[1:])
    print(json.dumps({"run_id": imported.run_id, "folder": str(folder), "events": dict(events), "trail_lines": len(imported.trail)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
