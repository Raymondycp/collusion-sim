"""Save the case presets in this folder as server presets (POST /api/presets). No paid call; a known name gets version + 1.

Usage: uv run --no-project python -I presets/cases/load.py [--server URL] [--dry-run]
The server URL comes from --server, else SIM_API_URL, else http://127.0.0.1:8000/api. A preset whose scenario_hash is
not the server's current hash of its scenario is not saved (the run would refuse it). On 23 Oct the server answers
POST /presets with 501 not_built (docs/sim/api/README.md); until the preset store is built, run a file with
`collusion-sim runs start --config presets/cases/<name>.json --allow-paid` (a paid run; the run name is case-<name>).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOTES = {
    "base": "Case preset (SCENARIO-LIBRARY §2): reference, neutral base prompt, private pair channel.",
    "amazon_posters": "Case preset: US v. Topkins (2015), poster sellers on Amazon Marketplace; private pair; permission sentence.",
    "airline_fares": "Case preset: US v. ATPCO (1992 to 1994), public fare filings; public channel only.",
    "shipping_gri": "Case preset: EU AT.39850 (2016), container GRI announcements; public channel only.",
    "parallel_only": "Case preset: Bell Atlantic v. Twombly (2007), parallel conduct; no channel.",
}


def call(server: str, method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{server}/{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        problem = json.loads(e.read() or b"{}")
        sys.exit(f"{method} /{path}: HTTP {e.code} {problem.get('code')}: {problem.get('message')}")
    except urllib.error.URLError as e:
        sys.exit(f"no answer from {server}: {e.reason}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--server", default=os.environ.get("SIM_API_URL", "http://127.0.0.1:8000/api"))
    ap.add_argument("--dry-run", action="store_true", help="Check the files and the scenario hashes; save nothing.")
    args = ap.parse_args()
    server = args.server.rstrip("/")
    for name, notes in NOTES.items():
        config = json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))
        current = call(server, "GET", f"scenarios/{config['scenario_id']}")["hash"]
        if current != config["scenario_hash"]:
            sys.exit(f"{name}: scenario_hash {config['scenario_hash']} is not the server's {current}; update the file")
        if args.dry_run:
            print(f"{name}: ok (dry run)")
            continue
        saved = call(server, "POST", "presets", {"name": name, "config": config, "notes": notes})
        print(f"{name}: {saved['id']} version {saved['version']}")


if __name__ == "__main__":
    main()
