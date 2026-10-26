# doc: papers, design notes, notebooks, figures

## Folder map

| Path | What it is |
|---|---|
| `paper/Index - Evidence of AI Agnets Collusion-draft.md` | The team's submission draft |
| `paper/paper-elastic.md` | The research draft: every number has a source tag (notebook section or table) |
| `paper/lab-notebook.md` | The dated lab notebook of the study: what was run, decided and found, in time order (the draft's time line cites it). Local user folders are written `<user>` |
| `design/CONDITIONS.md`, `design/conditions-comic.svg` | The six conditions, with an example of each (the comic is drawn by `conditions-comic.py`) |
| `design/SPEC.md` | The team member's specification of the measures (talk J, memory M, Dep, CA, Core) and of the case table |
| `design/Screenshot 2026-10-26 112823.png` | The web workbench on test PC-plus seed 312 |
| `notebooks/analysis.ipynb` | Dev (seeds 301-310): data engineering, checks, measures, the rule fit. Executed, all outputs inside |
| `notebooks/analysis-test.ipynb` | Test (seeds 311-320): the same notebook on the test data with the frozen rule; section C7 is the result |
| `notebooks/overview.ipynb` | All episodes first (scatter), then paired-by-seed statistics, then cases picked by a written rule; checks the numbers of the team draft against the tables |
| `notebooks/example.ipynb` | One test episode (C1 seed 311) worked by hand: every score recomputed from its inputs and checked against `C7_episode_rule.csv` |
| `notebooks/build_*.py` | The builders: each notebook is generated from its builder. Edit the builder, never the notebook |
| `notebooks/run_dev.sh` | Builds and executes `analysis*.ipynb` for one data set; replaces the notebook only after a clean run |
| `notebooks/rerun.py` | Reruns the analysis from this package (see below) |
| `notebooks/dump_outputs.py`, `notebooks/dumps/<set>.txt` | Every cell output of a notebook as text, for diff and review |
| `notebooks/check_draft.py` | Checks the research draft for pilot numbers |
| `notebooks/make_runs_check.py` | Writes `runs-check.csv` next to an export |
| `notebooks/package_study.py` | The script that built this package (it reads the project repository, not this folder) |
| `figures/<set>/` | Every figure the notebooks draw (PNG), by data set: `dev2`, `test`, `example`, `overview` |

## Notebook sections (`analysis*.ipynb`)

- **A. Data engineering and checks.** A0 inventory and file hashes; A0b seed index; A1 load the events; A2 tidy tables (runs, rounds, messages, calls); A3 integrity checks against the export counts; A4 manipulation checks from the events (channels, prompts, C3 isolation); A5 data quality and the broken-episode rule; A6 what the messages are; A7 measures without the judge (C, Dep, CA, Δp); A7b did prices rise (manipulation check of the harm label); A8 the LLM judge (J, M); A9 the episode table; A10 did agreement happen (offer and acceptance).
- **B. Descriptive figures.** B1 where the C1-public-private posts went; B2 one dot per episode; B3 price paths; B4 paired differences by seed.
- **C. The rule.** C1 measures per group; C2 distributions and paired differences; C3 AUC per score (single and drop-one); C4 threshold sweep (dev only); C5 talk against prices; C6 prices around the offer and the acceptance, C6b PC-plus price paths; C7 the frozen rule, the case table and the endpoint.
- **D. Draft check.**
- **E. Exploratory views** (not pre-specified).
- **F. Secondary views** decided after the test results (not pre-specified): recall by manipulation check, without broken episodes.

Each check prints one line: PASS, or FLAG (reported, never fixed in silence). Dev prints 9 FLAG lines and test 7; each is explained next to it.

## Rerun the analysis

Needs Python 3.11 with nbformat, jupyter, nbconvert, pandas, numpy, matplotlib, scipy, pyyaml, plotly, kaleido (PNG export, needs Chrome) and anthropic 1.17.0, and bash.

```
python doc/notebooks/rerun.py <that python> rerun-work all
```

- It copies the package into the project layout under `rerun-work/`, runs `run_dev.sh` for dev2 and test, and compares every table with `processed-data/`. One data set takes about 5 to 10 minutes.
- No paid call: the judge scores come from `raw-data/judge-cache/`. The script removes `JUDGE_ALLOW_PAID` from the environment.
- `example.ipynb` and `overview.ipynb` read the tables of `rerun-work/paper/analysis-elastic/out/`: build them there with `python build_example_notebook.py` and `python build_overview_notebook.py`, then `jupyter nbconvert --to notebook --execute --inplace <notebook>`.
