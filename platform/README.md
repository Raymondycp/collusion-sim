# platform: the experiment platform

The code that ran the experiment: a market simulator with LLM sellers, a REST and WebSocket API, a CLI (`collusion-sim`) and a web UI. It is copied from the project repository (tracked files only) with no run data. The only study data here is the frozen configuration of this study, in `studies/haiku-elastic/`.

## Folder map

| Path | What it is |
|---|---|
| `src/simcore/` | Simulation core: market and demand (`market.py`, `demand/`), sellers and their tool calls (`agents.py`, `tools.py`), shocks, the event recorder (`recorder.py`), branches (probe, placebo, ablation) |
| `src/adapters/` | LLM provider adapters (Anthropic Messages, OpenAI-style), call cache, budget |
| `src/api/` | FastAPI app: REST under `/api`, run replay over the WebSocket `/api/ws/runs/{run_id}` |
| `src/services/` | Runs, experiments, conditions, seeds, freeze, provider profiles, exports |
| `src/features/`, `src/indicators/` | Per-episode features (price index, best-response gap, ...) and per-round indicators |
| `src/judge/` | The platform's judge jobs (not used in this study: the notebooks judge the talk, see `doc/`) |
| `src/simschema/` | The episode event schema (`episode.v5`) |
| `src/simcli/` | The CLI `collusion-sim` |
| `web/` | Web UI source (React, Vite). `web/src/mocks/` is the mock mode used by the tests and by `static/` |
| `tests/` | Backend and CLI tests (`uv run pytest -q`); `web/tests/` are the UI tests |
| `presets/` | Market case presets |
| `scripts/` | Helper scripts (`host.py` starts backend and UI together; `fit_rule.py`, `plan_estimate.py`, ...) |
| `docs/sim/SPEC.md` | Simulator rules: the market, the round, the events, the conditions |
| `docs/sim/api/openapi.yaml` | The API contract (version 0.45.0); `docs/sim/api/README.md` is its change log; `examples/` holds small episode files |
| `studies/haiku-elastic/` | The study root of this experiment: `conditions/` and `scenarios/` (dictionary below) |
| `pyproject.toml`, `uv.lock`, `.python-version` | Python 3.11 project, managed with uv |
| `.env.example` | The settings the server reads from `.env` (no values). Never commit a `.env` |

## Data dictionary: `studies/haiku-elastic/`

| File | Content |
|---|---|
| `conditions/<condition>.yaml` | One condition: `id`, `labels` (legal, harm: from the design), `channels` (public board, private pair chat, news), `call_mode`, `c3_isolation` (true: the pricing call does not see the chat), `transparency`, prompt references, `status: frozen` and `frozen_hash` |
| `conditions/prompts.draft.yaml` | The prompt texts the conditions refer to |
| `conditions/seeds.yaml` | The seed map: the scenario generator inputs (26 to 35 rounds, ranges of the demand intercept a and the cost c, shock ranges) and each seed with its split (dev 301-310, test 311-320) |
| `conditions/models.yaml` | Seller model and settings (Claude Haiku 5.5, profile id `anthropic`; it names temperature 0.7, but the adapter did not send it: every call in `trail.jsonl` has empty `settings`, checked in notebook section A5) |
| `conditions/judge-rubric.yaml` | The talk and memory rubric for the 5 role judges (anchors 0 to 5, scored as 0.0 to 1.0) |
| `conditions/decision-rule.yaml` | The decision rule: evidence, scores, indicators, case table, flags, endpoint, and the thresholds fitted on dev (`fitted`) |
| `conditions/frozen.json` | The freeze record: per stage, the time and the SHA-256 of each frozen file. Stage `rule` frozen at 2026-10-25T13:30:18Z |
| `scenarios/scn_s<seed>.json` | The market of one seed: demand parameters, costs, shocks, rounds |

## Run it

Needs Python 3.11 with uv, and Node with pnpm for the UI.

```
uv sync
uv run collusion-sim serve --root studies/haiku-elastic --port 8000   # API; paid calls stay off unless SIM_ALLOW_PAID=1
pnpm --dir web install
SIM_BACKEND=http://127.0.0.1:8000 pnpm --dir web dev                  # UI on http://127.0.0.1:5173
```

`host.sh` (macOS, Linux) or `host.cmd` (Windows) starts both. A fresh study root has no runs: the server creates `data/` and `runs/` in it.

To run the study again (new paid calls, new episodes; the seeds fix the market, not the model sampling):
1. Copy `.env.example` to `.env` and set a provider profile for Anthropic (`SIM_PROFILE_ANTHROPIC_*`) and `SIM_ALLOW_PAID=1`.
2. Start the server with `--root studies/haiku-elastic`.
3. `uv run collusion-sim experiments estimate ...` and then `experiments start ...` with the conditions and seeds of `seeds.yaml` (see `uv run collusion-sim experiments start --help`), or use the Experiments page of the UI.
4. `uv run collusion-sim experiments export <id>` writes the zip that `raw-data/exports/` holds.

Tests: `uv run pytest -q` (backend and CLI), `pnpm --dir web test` and `pnpm --dir web typecheck` (UI).
