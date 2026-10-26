#!/usr/bin/env bash
# One command: start the backend and the web UI on localhost (macOS / Linux). Options: see scripts/host.py --help.
cd "$(dirname "$0")" || exit 1
exec uv run python scripts/host.py "$@"
