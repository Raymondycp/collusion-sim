# raw-data: the experiment exports and the judge replies

Everything here was written by a program, not by hand, and is never edited. The notebooks in `doc/notebooks/` read only these files (and the study configuration in `platform/studies/haiku-elastic/`).

## Folder map

| Path | Experiment | Conditions | Seeds | Episodes |
|---|---|---|---|---|
| `exports/dev2/` | `exp_01M5S0FH2S614XMN0Q0YBKG5TT` (dev) | C1, C1-public-private, C2a, C3-accept, C4 | 301-310 | 50 |
| `exports/dev2-pcplus/` | `exp_01M5RYA4EYY99257425HF40V9P` (dev) | PC-plus | 301-310 | 10 |
| `exports/test/` | `exp_01M5SM67A2DA6CHFMA5BM2HV2J` (test, run once after the freeze) | C1, C2a, C3-accept, PC-plus | 311-320 | 40 |
| `judge-cache/dev2/` | LLM-judge requests and replies for the 40 dev episodes with chat | | | |
| `judge-cache/test/` | LLM-judge requests and replies for the 30 test episodes with chat | | | |

The pilot (50 episodes before the final design) is left out: it shaped the design and is never cited.

## Each export folder

| File | Content |
|---|---|
| `experiment.zip` | The platform export (`collusion-sim experiments export`): `experiment.json` (the experiment: cells, conditions, seeds, budget), `manifest.json` (file list with hashes), and per run `runs/<run_id>/meta.json`, `episode.jsonl`, `trail.jsonl` |
| `features.csv` | The platform's feature table, one row per episode (from `GET /api/experiments/<id>/features`) |
| `features.csv.meta.json` | Definition of each feature column: id, version, name, description, evidence type |
| `runs-check.csv` | Counts per run made from `episode.jsonl` by `doc/notebooks/make_runs_check.py`; the notebooks compare them with their own parse |

### `meta.json` (one run)

`id`, `experiment_id`, `condition_id`, `seed`, `split`, `labels` (legal, harm), `scenario_id`, `max_rounds`, `models`, `status`, `end_reason` (`horizon` = all rounds played), `calls`, `cache_hits`, `fallbacks`, `clamps`, `cost_usd` (0: the profile has no price table), `config_hash`, times.

### `episode.jsonl` (one run, one event per line)

The first line is the header: `schema_version` (`episode.v5`), `run_id`, `condition_id`, `condition_hash`, `labels`, `label_source`, `split`, `seed`, `seeds_hash`, `scenario`, `channels`, `max_rounds`, `c3_isolation`. Every other line is an event with `seq` (order), `t` (round), `wall_time`, `type`:

| `type` | When | Main fields |
|---|---|---|
| `run_started`, `run_status_changed`, `run_ended` | start, status change, end | `status`, `end_reason`, `episode_hash`, `cost_usd`, `error` |
| `round_started` | each round | `round`, `reference` |
| `news_published` | a scheduled news item (demand or cost shock) | `text`, `visible` (which sellers see it) |
| `observed` | what a seller's call was shown | `agent_id`, `purpose` (talk or act), `observation` |
| `llm_call` | each model call | `agent_id`, `purpose`, `attempt`, `cache_hit`, `error`, `http_status`, tokens, `latency_ms`, `provider_profile_id` |
| `talk_replied` | the talk call's reply | `agent_id`, `channel_id`, `outcome`, `text` |
| `posted` | a message sent | `agent_id`, `channel_id` (`private_A_B`, `public`), `text`, `deliver_t` (round it is read), `receivers` |
| `planned`, `acted` | the pricing decision | `price`, `hold` (kept the last price), `fallback`, `forced` (no valid action), `asked_price`, `steps` |
| `settled` | the market clears | `prices`, `quantities`, `profits` per seller, `clamped` |
| `reference` | the round's benchmarks | `reference` (Nash and joint-optimum prices and profits, demand parameters, costs), `best_response` per seller, `consumer_surplus` |
| `snapshot_saved` | state saved for replay | `snapshot_hash` |

Full schema: `platform/src/simschema/` and `platform/docs/sim/SPEC.md`.

### `trail.jsonl` (one run, one model call per line)

The full request and reply of each seller call: `messages` (the prompt), `tools`, `settings`, `model`, `thinking`, `finish_reason`, tokens, `latency_ms`, `error`, `request_hash`, `cache_hit`. It holds no key: the key is added by the adapter at send time and never recorded.

## `judge-cache/<set>/`

The notebooks score the chat of each episode with 5 role judges (Claude Haiku 5.5): kind `j` (talk: how clearly the sellers agree to coordinate) and kind `m` (memory: per round, how clearly a seller's text continues the earlier conversation). Each score is 0 to 1 in steps of 0.1.

| File | Content |
|---|---|
| `<run_id>__<kind><k>__<prompt_hash>.json` | The accepted reply for one episode, kind and role k (0-4): `role`, `model`, `request` (the prompt), `system`, `raw` (the reply text), `parsed`, `rounded` (the score used), `valid`, `try_`, `usage`, `request_id` |
| `attempts.jsonl` | Every try, valid or not, in order (the cost and retry record) |

Only episodes with chat are judged (no chat: J = M = 0). Dev: 400 valid replies (40 episodes x 5 roles x 2 kinds); test: 300 valid replies of 308 tries (30 episodes). A rerun of the notebooks reads these files and makes no call.
