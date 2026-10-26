"""The Python test suite in fixed batches, one pytest process at a time, for a low-memory machine (DECISIONS-13 §4).

    uv run python scripts/test_all.py [--tests DIR] [--list] [--lock FILE] [--wait SECONDS]

It first takes a lock on FILE (default: one file in the temp folder, the same for every worktree of this user), so two
suites never run at once (DECISIONS-13 §6.6). It waits up to --wait seconds (default 600) for another suite, then exits 2.
The OS releases the lock when the process ends, so a crashed run leaves no stale lock. The holder writes its PID and start
time to FILE.holder; a waiter prints them. Its pytest processes inherit TEST_ALL_LOCK_HELD, so a test that runs this script
again does not wait for its own parent. The lock covers this script only: the web suite and a pilot server take no lock.

Each folder under the tests folder that holds test files is one batch. The test files directly in the tests folder go into
batches of BATCH files, in name order. It prints one summary line, then the id of each failed test and each batch that
gave no report or exited non-zero with no failed test (exit 5, no tests collected, is clean). Exit 1 when a test fails or
a batch fails so. --list prints the batches and runs nothing. The log folder is printed and kept; it is the caller's to
remove.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

BATCH = 6
LOCK = Path(tempfile.gettempdir()) / "collusion-sim-test-all.lock"
HELD = "TEST_ALL_LOCK_HELD"


def try_lock(f) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


@contextmanager
def machine_lock(path: Path, wait: float) -> Iterator[bool]:
    """Yields True with the lock held (or held by a parent test_all), False when `wait` seconds pass first."""
    if os.environ.get(HELD) == str(path):
        yield True
        return
    with path.open("a+b") as f:
        deadline, told = time.monotonic() + wait, False
        while not try_lock(f):
            if time.monotonic() >= deadline:
                yield False
                return
            if not told:
                print(f"test_all: another suite holds {path} ({holder(path)}); waiting up to {wait:.0f} s", file=sys.stderr, flush=True)
                told = True
            time.sleep(1)
        os.environ[HELD] = str(path)
        info = Path(f"{path}.holder")
        info.write_text(f"pid {os.getpid()}, started {time.strftime('%Y-%m-%d %H:%M:%S')}", encoding="utf-8")
        try:
            yield True
        finally:
            del os.environ[HELD]
            info.unlink(missing_ok=True)


def holder(path: Path) -> str:
    """The holder's PID and start time. A separate file: on Windows the locked byte cannot be read by another process."""
    try:
        return Path(f"{path}.holder").read_text(encoding="utf-8")
    except OSError:
        return "holder unknown"


def batches(tests: Path) -> list[list[Path]]:
    top = sorted(tests.glob("test_*.py"))
    out = [top[i : i + BATCH] for i in range(0, len(top), BATCH)]
    for d in sorted(p for p in tests.iterdir() if p.is_dir()):
        if any(d.rglob("test_*.py")):
            out.append([d])
    return out


def run(batch: list[Path], cwd: Path, xml: Path, log: Path) -> int:
    with log.open("wb") as f:
        args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", *map(str, batch)]
        return subprocess.run(args, cwd=cwd, stdout=f, stderr=subprocess.STDOUT).returncode


def main(argv: list[str]) -> int:
    tests = Path(argv[argv.index("--tests") + 1] if "--tests" in argv else "tests").resolve()
    plan = batches(tests)
    if "--list" in argv:
        for b in plan:
            print(" ".join(p.relative_to(tests.parent).as_posix() for p in b))
        return 0
    lock = Path(argv[argv.index("--lock") + 1]) if "--lock" in argv else LOCK
    wait = float(argv[argv.index("--wait") + 1]) if "--wait" in argv else 600
    with machine_lock(lock, wait) as held:
        if not held:
            print(f"test_all: {lock} is still held after {wait:.0f} s; another suite runs", file=sys.stderr)
            return 2
        return run_all(plan, tests)


def run_all(plan: list[list[Path]], tests: Path) -> int:
    out = Path(tempfile.mkdtemp(prefix="test-all-"))
    total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failed: list[str] = []
    start = time.monotonic()
    for n, batch in enumerate(plan, 1):
        xml, log = out / f"batch-{n}.xml", out / f"batch-{n}.log"
        code = run(batch, tests.parent, xml, log)
        if not xml.exists():
            failed.append(f"batch {n} gave no report (exit {code}): {log}")
            continue
        before = len(failed)
        for suite in ET.parse(xml).getroot().iter("testsuite"):
            for k in total:
                total[k] += int(suite.get(k, 0))
            for case in suite.iter("testcase"):
                if case.find("failure") is not None or case.find("error") is not None:
                    failed.append(f"{case.get('classname')}::{case.get('name')}")
        if code not in (0, 5) and len(failed) == before:
            failed.append(f"batch {n} exited {code} with no failed test: {log}")
    passed = total["tests"] - total["failures"] - total["errors"] - total["skipped"]
    print(
        f"test_all: {passed} passed, {total['failures']} failed, {total['errors']} errors, {total['skipped']} skipped"
        f" in {len(plan)} batches, {time.monotonic() - start:.0f} s (logs: {out})"
    )
    for f in failed:
        print(f"  {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
