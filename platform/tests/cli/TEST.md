# CLI tests: plan and results

Design: `docs/sim/CLI-DESIGN.md` §7. All tests use scripted runs only: no network, no paid call, no DBOS. Each test module has its own temporary `SIM_ROOT` with a copy of `scenarios/`.

How the tests reach the API: `conftest.py` builds `TestClient(create_app(root, use_dbos=False), base_url="http://testserver/api")` and gives it to the CLI as its HTTP client (`obj={"http": client}`). The CLI code under test is the production code; only the transport is different. Polling tests and transport-failure tests use `httpx2.MockTransport`.

Command until the baseline commit (lead decision, 21 Oct): `uv run --with click==8.5.0 pytest tests/cli`.

## Plan

### Unit 1 (core and read side)

| File | What it checks | Tests |
|---|---|---|
| `test_output.py` | `--json` before and after the subcommand gives the same body; human output is plain text; a Problem goes to stdout with `--json` and to stderr as one line; exit 1 for a Problem (`not_found`), exit 2 `cli_usage` for a missing argument, exit 3 `server_unreachable` for a refused connection and for a 5xx body that is not JSON; `not_built` routes pass through as exit 1 | 7 |
| `test_registries.py` | `health`, `demand-models list/show/solve`, `roles`, `tools`, `policies`, `generators`, `indicators list`, `ledger list/verify`: `--json` output equals the direct API body; `show` of an unknown id is `not_found`; a targets file that is not JSON is `file_invalid` with no request sent | 6 |
| `test_runs.py` | `runs list` (filters, `--all` across cursors), `show`, `events` (seq gap-free, `--types`, `--agent`), `events --follow --json` (JSON Lines that end with `run_ended`), `state --t` (and `snapshot_missing` past the end), `series`, `observations` | 7 |
| `test_follow.py` | the polling loop on a fake live run: sleeps while the run is live, stops at `run_ended`; with a `--types` filter that hides `run_ended`, stops after the run is terminal and one more page | 2 |
| `test_branches_indicators.py` | `branches list` shows the probe children; `indicators series --until 10` equals the first 10 points of the full series; bad `--params` gives `indicator_params_invalid` (exit 1) and non-JSON `--params` gives `file_invalid` | 3 |
| `test_contract.py` | each command's `--json` output validates against the contract schema of its path (parametrised) | 1 × 14 |
| `test_offline.py` | `episode validate` and `summary` pass on `docs/sim/api/examples/*.jsonl`; a seq gap, a missing `run_ended`, a bad event line and an empty file each give `episode_invalid` (with the line for a bad event); no file is written | 5 |

### Unit 2 (write side, CR-3 m1–m3, CLI-Hub keys, `--dry-run`)

| File | What it checks | Tests |
|---|---|---|
| `test_output.py` (added) | `error`/`type` keys: copies of `message`/`code`, the server's `type` URI kept; Ctrl-C gives `cli_aborted` exit 1; an id with `/`, `?` or `#` is one encoded path segment and gives `not_found` | +3 |
| `test_follow.py` (added) | a paused run: events so far, then `run_not_live` as the last JSON line; `--max-wait` gives `wait_timeout` (fake clock) | +2 |
| `test_scenarios.py` | create → check → freeze (`--approved-by` required) → replace gives `scenario_frozen`; replace and delete a draft; generate saves nothing, `--save` stores it, unknown generator; a paid generator sends no POST without `--allow-paid` and the server switch still refuses with it; `--dry-run` saves nothing; `reference --until` and `market preview` | 6 |
| `test_write_runs.py` | `start --wait` (2 seeds, final summaries, human output); an LLM config: `paid_flag_required` with no run, `paid_calls_disabled` from the server with `--allow-paid`, `--dry-run` starts nothing; pause, `--follow` gives `run_not_live`, resume, then `stop` of the ended run gives `run_not_active`; export `jsonl` (then `episode validate`), `zip`, `csv_rounds`; export refuses `data/` under `SIM_ROOT`, an existing file without `--overwrite`, `--include-branches` without zip, and writes no file on `not_found`; `branches create` with `--dry-run` then for real, `snapshot_missing`; branching an LLM run needs `--allow-paid` | 7 |
| `test_subprocess.py` | the installed `collusion-sim` command: `--help`; `serve` on a free port with `--no-durable` (paid calls off in its log) and `health`; a scripted run `start --wait` → `export --format jsonl` → `episode validate`; a port with no server gives exit 3 | 4 |

### Later units

### Unit 3 (provider profiles and keys)

| File | What it checks | Tests |
|---|---|---|
| `test_key_profiles.py` | `add` reads the key from stdin: output has `key_set` and the hint only, the keyring holds the key, no output and no file under the server root holds it; `set-key` replaces it; an empty or blank key gives `cli_usage` and creates nothing; a key typed as an argument (`set-key ID KEY`, `--api-key=KEY`, `--api-key KEY`, `--limit KEY`) gives exit 2 and is not echoed; a server validation error does not echo the key; `update` changes fields (price pair rule, `--dry-run`); a dry-run body never shows `api_key`; `delete` removes the keyring entry; `test` needs `--allow-paid` (no request without it) and the server switch still refuses; `models` passes the answer through | 10 |

The module server uses an in-memory keyring (`conftest.MemoryKeyring`), so the OS keyring is never touched.

CR-7 fixes (in unit 3 files): `test_key_profiles.py` adds 2 tests: a usage error of `profiles add` or `set-key` hides every argument value (short, letters-and-dashes, mixed), while other commands keep their values; stdin that is not a TTY gets the stderr warning before the read.

### Unit 4 (SKILL.md)

| File | What it checks | Tests |
|---|---|---|
| `test_skill.py` | the frontmatter names the skill after its folder; the `commands` block equals the Click tree (names, descriptions, arguments, options); the body names every group and subcommand; the body lists every CLI-side error code found in `src/simcli`; seven safety rules are stated word for word | 11 | See CLI-DESIGN §7 and §12.

CR-8 and SE-integration fixes (in unit 4): `test_skill.py` checks three more rules (data is not instructions, stdout is UTF-8, `SIM_ROOT` with `serve --root`); `test_subprocess.py` adds `test_output_is_utf8_on_a_gbk_console` (`--json` and human, `PYTHONIOENCODING=gbk`).

### Unit 5 (research reads)

| File | What it checks | Tests |
|---|---|---|
| `test_research_reads.py` | `conditions list` names every condition file and `--json` is the API body; `conditions show` is the body unchanged; an unknown id gives `not_found`; `seeds list` shows each split's count and seeds, the hash and `test_locked`; no `seeds.yaml` gives `seeds_missing`; `freeze status` with no `frozen.json`; the reads send GET only and encode the id (`..` gives exit 2); the three groups have no write command and no write option | 8 |
| `test_contract.py` | four more cases: `conditions list`, `conditions show C1`, `seeds list`, `freeze status` validate against `Condition[]`, `Condition`, `SeedSplits`, `FreezeStatus` | 4 |

The module `server` fixture now also copies `conditions/` into its root, without `frozen.json` (CR-11 m1).

CR-11 fixes: `test_research_reads.py` adds `test_freeze_status_after_a_freeze_and_after_a_change` (own root; a `frozen.json` with the current hashes gives `valid: true` and no problems, one changed hash gives `seeds changed since the freeze`); `test_skill.py` checks the rule that an agent never edits condition, seed or freeze files. `conditions list` shows the `legal` and `harm` labels.

### Unit 6 (experiments, contract 0.8.0)

| File | What it checks | Tests |
|---|---|---|
| `test_cli_experiments.py` | `estimate` is the API body, validates against `ExperimentEstimate` and stores nothing; human `estimate` has the totals; a scripted pilot experiment (C1, seed 1, default branch plan) from `start --wait` to `completed`, validated against `Experiment`, `show` equals the API body, `list` items validate against `ExperimentSummary`, the human cell line; an `llm` agent in `agents` or `agents_by_condition` needs `--allow-paid` and nothing is sent; `--dry-run` prints the request and sends nothing; `--allow-paid` still meets the server switch (`paid_calls_disabled`); `research_field_locked` and `seed_not_in_split` pass through; `--wait` stops on a paused run (`run_not_live`), after `--max-wait` (`wait_timeout`), ends on `ended`, stops on `blocked` with one `errors` entry per blocked cell; `show` encodes the id, shows the cell error code, and an unknown id is `not_found`; `export` writes the served zip (experiment.json first, manifest.json last, a trail.jsonl per run), its summary has the path, bytes and sha256, an existing file is `path_refused`, an unknown id is `not_found` with no file (DECISIONS-14 §2.4) | 16 |
| `test_research_reads.py` | `seeds list` with `seeds[]`: every seed row (split, `shock_news`, informed seller, scenario) and the balance on each split line; without `seeds[]` the old split table | 2 |

The module was named `test_experiments.py` first; it clashed with de's `tests/test_experiments.py` (pytest import mismatch), so it is `test_cli_experiments.py`.

### Unit 7 (run-config schema)

| File | What it checks | Tests |
|---|---|---|
| `test_cli_schema.py` | `--json` is the API body and a valid 2020-12 schema; the CLI test config and the team example pass it; the summary has every field with its required and locked flags, every locked field and exactly the server's choices; every policy kind, tool and indicator prints its params schema; `--def`; unknown choices and two parts at once are usage errors; one GET only. `--check`: good exploratory and research configs (also `memory.summary`) pass and the server accepts them (202); schema problems come with `/` paths and `(root)`; in a research config the locked fields that `--check` finds equal the paths of the server's `research_field_locked`; a bad or missing file is `file_invalid`; one GET and no write; `--check` with another part is a usage error | 22 |

### Unit 8 (estimate, defaults, .env profiles)

| File | What it checks | Tests |
|---|---|---|
| `test_cli_estimate_defaults.py` | `runs estimate`: the API body, validates against `CostEstimate`, stores no run; an `llm` config needs no `--allow-paid` and sends one POST with the file unchanged; no write options; human totals, rate-window row, warnings and assumptions (contract example); a 422 at `agents/0/policy/llm` passes through; a bad or missing file is `file_invalid` and sends nothing. `defaults`: the API body with `SIM_DEFAULT_*` set, validates against `Defaults`; one line per role and agent; one GET. `.env` profile (`SIM_PROFILE_ENVTEST_*` in the server env): `profiles update`, `set-key` and `delete` give `profile_read_only` and no key text | 12 |
| `test_research_reads.py` (unit 8) | `freeze run` on contract 0.13.0: stage `conditions` makes `stages.conditions` valid and leaves the rule stage open; stage `rule` then makes the freeze valid, `frozen.json` names the approver, and the rule stage's hashes (with `decision_rule`, `judge_rubric`) equal the response; rule before conditions is 409 `conflict` and writes nothing; the judge rubric freezes with stage conditions (its hash is in that stage, the decision rule's is not); stage conditions without `judge-rubric.yaml` is 409 `freeze_incomplete` and writes nothing; stage rule without `decision-rule.yaml` is 409 `freeze_incomplete` naming only that file, and `frozen.json` keeps the conditions stage; one POST with `{stage, approved_by}`, and `--dry-run` sends nothing; no stage, no approver or an unknown stage is a usage error. `freeze status` after a change: both stages through the CLI, then a stored seed hash changed. The read-only check allows only `freeze run` to write | 8 new (3 parametrized), 2 changed |
| `test_cli_features.py` | `features export` on a finished scripted experiment (C1, seed 1, two constant sellers): the json file equals the served bytes, validates against `FeatureTable`, and the summary gives `features_code_hash`, sha256, rows; `corr_raw` and `corr_lag` stay null with `flat_prices`; csv equals the served csv, a null is an empty cell; layer A has fewer features than B with the same code hash; two exports have the same sha256; human summary; a 404 or 409 `test_split_locked` passes through with no file written; an existing file or a server folder is `path_refused` before any request; no write options. CR-25 m2: a csv export sends one request (`format=json`), the csv equals the served csv, and `PATH.meta.json` equals the json `meta` with each feature's version, layer and code hash; an existing `.meta.json` is `path_refused` without `--overwrite` | 11 |

### Evaluations (contract 0.15.0)

| Module | What it checks | Tests |
|---|---|---|
| `test_cli_evaluations.py` | Two finished C1 pilot runs (scripted talkers, seeds 1, 2) and a test rubric. `estimate` is the API body, validates against `EvaluationEstimate`, gives 2 episodes and 6 scripted calls, and stores no job; human output lists an unknown run in `excluded`. A scripted job from `start --wait` to `show`: `completed`, `counts {ok: 6}`, valid against `Evaluation`. Paid guard for `llm` and `jev` with nothing sent; with `--allow-paid` and paid calls off, `paid_calls_disabled` and no llm job. `--dry-run`; `wait_timeout` names the job and the id is on stderr first (CR-39 m1); `show` encodes the id, unknown is `not_found`. `resume`: the paid guard reads the stored job and sends only that read; `--dry-run`; the real route refuses a job that did not fail (409 `conflict`) (CR-39 m2). A job that ends `failed` under `--wait` is exit 1 `evaluation_failed` with the job's error, for `start` and `resume` (CR-39 note). A JSON file that starts with a UTF-8 BOM (PowerShell 5.1) is read; the loader serves every `--file` and `--config` (CR-44 4) | 13 |

## Results

### Unit 1 (21 Oct 2026)

`uv run --with click==8.5.0 pytest tests/cli -v`: **56 passed in 4.1 s** (the parametrised cases count one each).

```
tests/cli/test_branches_indicators.py ...                                [  5%]
tests/cli/test_contract.py .................                             [ 35%]
tests/cli/test_follow.py ..                                              [ 39%]
tests/cli/test_offline.py ......                                         [ 50%]
tests/cli/test_output.py .......                                         [ 62%]
tests/cli/test_registries.py ..............                              [ 87%]
tests/cli/test_runs.py .......                                           [100%]
```

Full suite `uv run --with click==8.5.0 pytest`: 337 passed in 57 s. `uv run ruff check src/simcli tests/cli`: clean.

Defect checks (each made by hand, then reverted): removing the "one more page after the run is terminal" step in `--follow` fails `test_follow_with_a_filter_…`; giving `server_unreachable` exit 1 in place of 3 fails two tests in `test_output.py`.

Coverage notes: no subprocess test yet (unit 2, it needs the entry point). `runs events --follow` against a live run of the real server is covered only through the fake transport (`test_follow.py`); the real-server test reads a finished run.

### Unit 2 (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **78 passed in 23 s** (unit 1: 56, now 61 after the added cases; unit 2 modules: 17). `uv run pytest`: 376 passed in 89 s. `uv run ruff check src tests` and `ruff format --check src tests`: clean.

The subprocess module starts one server with `collusion-sim serve` and stops it at the end. A separate check showed that terminating the `collusion-sim.EXE` launcher closes the server port (no orphan process).

### Unit 3 (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **88 passed** (unit 3: 10). Defect checks (made by hand, then reverted): without the usage-error redaction, `test_a_key_typed_as_an_argument_is_refused_and_not_echoed` fails; without the dry-run `api_key` mask, `test_key_commands_have_no_dry_run_and_a_dry_run_never_shows_a_key` fails.

### Unit 4 and CR-7 (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **101 passed** (unit 4: 11; CR-7: 2). `ruff check src tests` and `ruff format --check src tests`: clean. Defect check: an extra option added to the SKILL.md `commands` block fails `test_commands_block_equals_the_click_tree`. The skill is listed by Claude Code as `collusion-sim-cli`.

### CR-8 fixes (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **106 passed**. Defect check: with the UTF-8 reconfigure removed, both `test_output_is_utf8_on_a_gbk_console` cases fail.

### Unit 5 (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **118 passed** (unit 5: 8 + 4 contract cases). `ruff check src/simcli tests/cli` and `ruff format --check src/simcli tests/cli`: clean. Defect check: without `seg()` in `conditions show`, `test_research_reads_send_only_gets_and_encode_the_id` fails.

### CR-11 fixes (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **133 passed** (118 + 1 freeze test + 1 safety rule + 13 in `test_cli_experiments.py`, which tests the experiments group on a local `SimGroup` until it is registered). `ruff check` and `ruff format --check` on `src/simcli tests/cli`: clean. Full suite collects (508 tests) after the rename of `tests/cli/test_experiments.py` to `test_cli_experiments.py`.

### Unit 6 and CR-16 fixes (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **155 passed** (unit 6: 17; CR-16: 2 tests in `test_cli_experiments.py` and 2 rules in `test_skill.py`; `test_cli_schema.py` 13 is unit 7, on a local group). `ruff check` and `ruff format --check` on `src/simcli tests/cli`: clean.

### CR-20 m2 (21 Oct 2026)

SKILL.md says that `--check` skips the rules between fields and that `ok: true` is not the server's acceptance; `test_skill.py` checks the sentence. m1 (`jsonschema` as a runtime dependency) waits for the `pyproject.toml` change after de's item 2 commit.

### CR-20 m1 (21 Oct 2026)

`jsonschema==4.26.0` moved from the dev group to `[project] dependencies` (`uv add`, `uv remove --dev`; lead approved after f138956). `schema.py` imports it at the top. `test_check_needs_no_dev_dependency` checks the installed package's runtime requirements.

### Unit 8 (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **180 passed** (unit 8: 12). `ruff check` and `ruff format --check` on `src/simcli tests/cli`: clean.

Unit 8 final (21 Oct 2026), in the main working tree with de's uncommitted unit 3 and contract 0.13.0 (two freeze stages): see the result line below. Tests changed for de's unit 3: the freeze tests use `freeze run --stage`; the experiments test app has an env profile (`envtest`), so `--allow-paid` still meets `paid_calls_disabled` after the binding check; `test_scenarios.py::test_draft_check_freeze_flow` passes again after de restored `{approved_by}` on `POST /scenarios/{id}/freeze`.

`uv run pytest tests/cli -o addopts="" -q`: **199 passed** (with the judge rubric in stage conditions). `ruff check` and `ruff format --check` on `src/simcli tests/cli`: clean.

### Evaluations unit (21 Oct 2026)

`uv run pytest tests/cli -o addopts="" -q`: **208 passed** (evaluations: 9). `ruff check` and `ruff format --check` on `src/simcli tests/cli`: clean. Defect check: with the `require_paid` call in `evaluations start` removed, both `test_a_paid_backend_needs_allow_paid_and_nothing_is_sent` cases fail.

### `features export --evaluation` (22 Oct 2026, contract 0.17.0)

`test_cli_features.py::test_evaluation_is_passed_as_evaluation_id_and_an_unknown_one_writes_no_file`: against the real server, an unknown evaluation is `not_found` and writes no file (so the J4 route reads the parameter). Against a fake transport, the query is `{layer, format, evaluation_id}`, the file is the served bytes, and the summary has `evaluation_id` only when the option is given. `uv run pytest tests/cli/test_cli_features.py -o addopts="" -q`: 12 passed.

### CR-39 m1-m3 (22 Oct 2026)

- m1: `test_wait_gives_up_after_max_wait` now checks that stderr starts with `evaluation eval_1 running; waiting`, and that the `wait_timeout` body has `evaluation_id` and names the id in `message`.
- m2: `test_resume_of_a_paid_job_needs_allow_paid_and_sends_only_the_read` (fake transport): a stored `llm` job without `--allow-paid` is `paid_flag_required` after one GET; with `--allow-paid --dry-run` it prints `POST /evaluations/eval_1/resume` and sends only the GET. `test_resume_reaches_the_route_and_the_server_refuses_a_job_that_did_not_fail` (real server): a completed scripted job gives 409 `conflict`; an unknown id gives `not_found`.
- m3: two judge rules added to `test_safety_rules_are_stated`.

### CR-39 notes (22 Oct 2026)

`test_wait_on_a_job_that_ends_failed_is_exit_1_with_its_error` (fake transport): `start --wait` and `resume --wait` on a job that ends `failed` give exit 1 with `evaluation_failed`, `evaluation_id` and `evaluation_error` (the job's `{code, message}`). `uv run pytest tests/cli -o addopts="" -q`: 209 passed, 5 errors, all in the `test_subprocess.py` server fixture; that file rerun alone: 6 passed.

CR-64 (22 Oct): `test_cli_features.py::test_each_evaluation_is_passed_as_evaluation_id_and_an_unknown_one_writes_no_file` replaces the test above. Against a fake transport, `--evaluation eval_1 --evaluation eval_2` sends `evaluation_id` twice, in order (`layer`, `format`, `evaluation_id`, `evaluation_id`), and the summary has `evaluation_ids` (a list) only when the option is given. The merge rule itself is tested on the server side (`tests/test_judge_features.py`). `uv run pytest tests/cli/test_cli_features.py -o addopts="" -q`: 12 passed.
