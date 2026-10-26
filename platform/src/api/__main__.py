"""uv run python -m api  (binds 127.0.0.1:8000 only; SIM_ROOT = repo root, SIM_DURABLE=0 to run without DBOS)."""

import os

import uvicorn

from api.app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("SIM_PORT", "8000")), log_level="info")
