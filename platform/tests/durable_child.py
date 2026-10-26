"""Child process for test_durable: `crash` dies inside round 12 of a DBOS workflow, `recover` relaunches DBOS."""

import sys
from pathlib import Path

import test_durable
from adapters import durable
from adapters.store import RunsDB, Writer
from test_durable import CLOCK, parts

root, mode = Path(sys.argv[1]), sys.argv[2]


test_durable.CrashingGrim.hard = True


def builder(run_id):
    return parts(crash=mode == "crash")


db = RunsDB(root / "runs.sqlite")
writer = Writer(db)
durable.launch(root / "dbos.sqlite", lambda run_id: durable.resume_or_start(db, writer, builder(run_id), root / "runs", now=lambda: CLOCK))
try:
    if mode == "crash":
        durable.start("run_TEST").get_result()
    else:
        entry = durable.retrieve("run_TEST").get_result()
        print(f"status={entry['status']}")
finally:
    durable.shutdown()
    writer.close()
