# Evidence of collusion between AI sellers: the Haiku elastic study

Apart AI Collusion Research Sprint (23-26 Oct 2026), Track 2, question 2.3: "What should count as convincing evidence of collusion?"

Two LLM sellers (Claude Haiku 5.5, `claude-haiku-5-5`, the model's default sampling: `models.yaml` names temperature 0.7, but no temperature was sent, as the empty `settings` of every recorded call shows) set prices in a repeated market. In some conditions they can chat. A decision rule reads three kinds of evidence from each episode (what the sellers say, whether their prices move together, whether they earn more than competition gives) and flags the episode. The rule was fitted on dev seeds, frozen, and applied once to held-out test seeds.

**Start here:** open [`doc/notebooks/overview.ipynb`](doc/notebooks/overview.ipynb). Its first cell maps each finding of the paper to the notebook section, the table and the figure that support it. The executed notebooks open in any Jupyter viewer, with no rerun.

## Folders

| Folder | What it holds | Start with |
|---|---|---|
| [`platform/`](platform/README.md) | The experiment platform code: backend, CLI `collusion-sim`, web UI source, tests, and the frozen configuration of this study. No run data | `platform/README.md` |
| [`raw-data/`](raw-data/README.md) | The three experiment exports as the platform wrote them (every event of every episode), and the LLM-judge replies | `raw-data/README.md` |
| [`processed-data/`](processed-data/README.md) | The tidy tables made from the raw data by the notebooks (one row per episode, round, message ...). Use these for any new analysis | `processed-data/README.md` |
| [`doc/`](doc/README.md) | The paper drafts, the design notes, the notebooks (with their builders and a rerun script) and every figure | `doc/README.md` |
| [`static/`](static/README.md) | A read-only copy of the web UI that replays every run without the server | `static/README.md` |
| `KEY-SCAN-REPORT.txt` | Proof that no API key is in this folder (section "Keys" below) | |

## The experiment in one table

| Item | Value |
|---|---|
| Market | Linear demand q_i = a - b p_i + d p_j; b = 3, d = 2.5; a = 10 and unit cost c = 1, each drawn per seed in 9.5-10.5 and 0.9-1.1; at a = 10 and c = 1 the one-shot Nash price is 3.714 and the joint-profit optimum 10.5; demand and cost shocks per seed |
| Episode | 2 sellers, 26 to 35 rounds (set per seed); each round: optional chat, then each seller sets a price |
| Conditions | C1 (private chat, may coordinate), PC-plus (told to agree on a high price: positive control), C3-accept (chat, but the pricing call cannot see it), C2a (no chat); dev only: C1-public-private, C4. Details: `doc/design/CONDITIONS.md` |
| Labels | From the design, never from the outcome. Harm standard: C1, PC-plus (and C1-public-private, C4) positive; C3-accept, C2a negative. Legal standard: C1, PC-plus, C3-accept positive; C2a negative |
| Dev | seeds 301-310: 60 episodes (6 conditions x 10). The rule thresholds were fitted here |
| Freeze | 2026-10-25 13:30:18 UTC (`platform/studies/haiku-elastic/conditions/frozen.json`) |
| Test | seeds 311-320: 40 episodes (4 conditions x 10), started 13:32:20 UTC, run once |
| Judge | Claude Haiku 5.5 with 5 role prompts scores the chat (talk J, memory M), 0 to 1 in steps of 0.1 |
| Result (test, harm standard) | Primary flag: recall C1 1/10, PC-plus 0/10; false alarms C3-accept 0/10, C2a 0/10. Secondary flag: C1 6/10, PC-plus 4/10, C3-accept 6/10, C2a 0/10. Table `processed-data/test/C7_endpoint_by_group.csv`, figure `doc/figures/test/C7_endpoint_by_group.png` |

## Rules for a new analysis

- Labels come from the design. The manipulation check (did prices rise, Δp >= 0.10) is reported separately, never used as a label.
- Explore on dev only. The test seeds were read once; a pattern found on test is a description, not a confirmation.
- Report each group on its own (k of n with a 95% Clopper-Pearson interval); never a pooled accuracy. Resample by seed: the seeds pair the conditions.
- Do not change `raw-data/`. Write new code in a new file.

## Rerun

- The analysis: `python doc/notebooks/rerun.py <python>` rebuilds and runs the notebooks from `raw-data/` and checks every table against `processed-data/`. No paid call. See `doc/README.md`.
- The experiment itself: `platform/README.md`. It makes new paid model calls and gives new episodes.

## Keys

No `.env`, provider profile or API key is in this folder. `KEY-SCAN-REPORT.txt` lists the scan of every file and every member of every zip:
1. Secret patterns (an Anthropic key prefix with key characters, any long `sk-` key, a JSON api-key field with a value, a key hint with a value, a key header with a value, a Bearer token, the Anthropic key variable with a value). Result: 0.
2. The key, token and password values of the project `.env`, whole or their first 12 or last 8 characters, compared inside the script and never printed. Result: 0.
3. The words `sk-ant`, `api_key`, `ANTHROPIC`, `key_hint`, `key_set`, `key_store`, `provider_profile`, `SITE_PASSWORD`, with their contexts for a reader to judge. They occur as the provider name (`provider_profile_id: anthropic`), the SDK name, and the documentation of the key variable.

## Licence and sources

The platform code and the analysis are the team's work. The demand formula follows ColludeBench (MIT); no ColludeBench or Colosseum code is included.
