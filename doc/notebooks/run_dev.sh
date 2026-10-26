#!/usr/bin/env bash
# Build and run the elastic-study notebook for one dataset (ANALYSIS_DATASET: dev2, the default; pilot; test). DECISIONS-36 §4.
# Usage, from the repository root: ANALYSIS_PY=<venv python> [ANALYSIS_DATASET=dev2] bash paper/analysis-elastic/run_dev.sh
# The python needs: nbformat, jupyter, nbconvert, pandas, matplotlib, scipy, pyyaml, plotly, kaleido (PNG export, needs Chrome) and anthropic==1.17.0 (the judge call).
# Paid judge calls only when JUDGE_ALLOW_PAID=1 and ANTHROPIC_API_KEY are set in the environment.
set -eu
PY=${ANALYSIS_PY:?set ANALYSIS_PY to a python with nbformat, jupyter, nbconvert, pandas, matplotlib, scipy, pyyaml}
DS=${ANALYSIS_DATASET:-dev2}
if [ "$DS" = pilot ]; then OUTD=dev; else OUTD=$DS; fi
export PYTHONIOENCODING=utf-8 ANALYSIS_ROOT=$(pwd -W 2>/dev/null || pwd) ANALYSIS_DATASET=$DS
if [ "$DS" = dev2 ]; then NB=analysis.ipynb; else NB=analysis-$DS.ipynb; fi
D=paper/analysis-elastic
# Build and execute into temp files; only a clean execution replaces $NB, so a failed or stopped run never leaves it unexecuted (AUDIT-E20-1, E20-10).
"$PY" $D/build_notebook.py "$D/_build-$DS.ipynb"
timeout 10800 "$PY" -m jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=-1 --output "$(pwd)/$D/_exec-$DS.ipynb" "$D/_build-$DS.ipynb"
mv -f "$D/_exec-$DS.ipynb" "$D/$NB"
mkdir -p "$D/out/$OUTD"
"$PY" $D/dump_outputs.py "$D/$NB" 10000000 > "${DUMP:-paper/analysis-elastic/out/$OUTD/dump.txt}"
echo "FLAG lines: $(grep -c FLAG "${DUMP:-paper/analysis-elastic/out/$OUTD/dump.txt}")"
