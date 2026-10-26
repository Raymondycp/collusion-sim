"""scripts/check_text.py and scripts/test_all.py (DECISIONS-13 §3, §4.1), run as the lead runs them, on throwaway repos."""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK, TEST_ALL = ROOT / "scripts" / "check_text.py", ROOT / "scripts" / "test_all.py"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True)


def script(path: Path, cwd: Path, *args: str) -> tuple[int, list[str]]:
    r = subprocess.run([sys.executable, str(path), *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout.splitlines()


def repo(tmp_path: Path, files: dict[str, bytes]) -> Path:
    git(tmp_path, "init", "-q")
    for name, data in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(data)
    git(tmp_path, "add", "-A")
    return tmp_path


def test_check_text_lists_each_crlf_and_bom_in_the_index_or_working_tree_and_skips_binary_files(tmp_path):
    r = repo(
        tmp_path,
        {
            "ok.md": b"a\nb\n",
            "crlf.md": b"a\r\nb\r\n",
            "bom.md": b"\xef\xbb\xbfa\n",
            "staged_only.md": b"a\r\n",
            "image.png": b"\x89PNG\r\n\x1a\n\x00\x00\r\n",
        },
    )
    git(r, "commit", "-qm", "base")
    (r / "staged_only.md").write_bytes(b"a\n")  # the index still holds the CRLF that would be committed
    (r / "later.md").write_bytes(b"ok\n")
    git(r, "add", "later.md")
    (r / "later.md").write_bytes(b"\xef\xbb\xbfok\r\n")  # staged clean, then broken in the working tree only
    code, out = script(CHECK, r, "--no-ruff")
    assert code == 1
    assert out == [
        "bom.md: BOM in the index",
        "crlf.md: CRLF in the index",
        "later.md: CRLF in the working tree",
        "later.md: BOM in the working tree",
        "staged_only.md: CRLF in the index",
        "check_text: 5 problem(s)",
    ]


def test_check_text_passes_a_clean_repo_and_fails_on_unformatted_python(tmp_path):
    r = repo(tmp_path, {"src/good.py": b"x = 1\n", "README.md": b"ok\n"})
    assert script(CHECK, r) == (0, ["check_text: clean"])
    (r / "src" / "bad.py").write_bytes(b"x=1\n")
    git(r, "add", "-A")
    code, out = script(CHECK, r)
    assert code == 1 and out[-1].startswith("check_text:") and any(line.startswith("ruff format:") and "bad.py" in line for line in out)


def test_check_text_fails_on_a_ruff_lint_error(tmp_path):
    r = repo(tmp_path, {"src/good.py": b"x = 1\n", "src/unused.py": b"import os\n"})
    code, out = script(CHECK, r)
    lint = [line for line in out if line.startswith("ruff check:")]
    assert code == 1 and any("F401" in line for line in lint) and any("unused.py" in line for line in lint)


def test_test_all_runs_each_batch_and_prints_one_summary_line_with_the_failed_test(tmp_path):
    tests = tmp_path / "tests"
    (tests / "sub").mkdir(parents=True)
    (tests / "test_a.py").write_text("def test_a():\n    pass\n")
    (tests / "test_b.py").write_text("def test_b():\n    assert False\n")
    (tests / "sub" / "test_c.py").write_text("import pytest\n\n\ndef test_c():\n    pass\n\n\n@pytest.mark.skip\ndef test_d():\n    pass\n")
    assert script(TEST_ALL, tmp_path, "--tests", str(tests), "--list") == (0, ["tests/test_a.py tests/test_b.py", "tests/sub"])
    code, out = script(TEST_ALL, tmp_path, "--tests", str(tests), "--lock", str(tmp_path / "lock"))
    assert code == 1
    assert out[0].startswith("test_all: 2 passed, 1 failed, 0 errors, 1 skipped in 2 batches")
    assert out[1:] == ["  tests.test_b::test_b"]


def test_test_all_fails_a_batch_that_exits_non_zero_with_no_failed_test_or_no_report(tmp_path):
    tests = tmp_path / "tests"
    for d in ("exit", "kill", "ok"):
        (tests / d).mkdir(parents=True)
    (tests / "exit" / "test_e.py").write_text('import pytest\n\n\ndef test_e():\n    pytest.exit("stop", returncode=3)\n')
    (tests / "kill" / "test_k.py").write_text("import os\n\nos._exit(1)\n")
    (tests / "ok" / "test_o.py").write_text("def test_o():\n    pass\n")
    code, out = script(TEST_ALL, tmp_path, "--tests", str(tests), "--lock", str(tmp_path / "lock"))
    assert code == 1 and out[0].startswith("test_all: 1 passed, 0 failed")
    assert out[1].startswith("  batch 1 exited 3 with no failed test: ") and out[2].startswith("  batch 2 gave no report (exit 1): ")
    assert len(out) == 3


def test_test_all_batches_hold_every_test_file_of_the_suite_once():
    code, out = script(TEST_ALL, ROOT, "--list")
    listed = [p for line in out for p in line.split()]
    files = [p for p in listed if p.endswith(".py")]
    assert code == 0 and "tests/cli" in listed and len(files) == len(set(files))
    assert sorted(files) == sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py"))


def test_test_all_waits_for_another_suite_and_a_killed_holder_leaves_no_lock(tmp_path):
    tests, lock = tmp_path / "tests", tmp_path / "lock"
    tests.mkdir()
    (tests / "test_a.py").write_text("def test_a():\n    pass\n")
    hold = (
        f"import sys, time; sys.path.insert(0, {str(TEST_ALL.parent)!r}); import test_all\n"
        f"with test_all.machine_lock(test_all.Path({str(lock)!r}), 0) as held:\n    print(held, flush=True); time.sleep(300)"
    )
    holder = subprocess.Popen([sys.executable, "-c", hold], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "True"
        r = subprocess.run([sys.executable, str(TEST_ALL), "--tests", str(tests), "--lock", str(lock), "--wait", "2"],
                           cwd=tmp_path, capture_output=True, text=True, timeout=60)  # fmt: skip
        assert r.returncode == 2 and "another suite holds" in r.stderr and "test_all:" not in r.stdout
        assert re.search(r"\(pid \d+, started \d{4}-\d\d-\d\d \d\d:\d\d:\d\d\)", r.stderr), "the waiter names the holder"
        nested = os.environ | {"TEST_ALL_LOCK_HELD": str(lock)}  # a test inside a suite that holds this lock
        r = subprocess.run([sys.executable, str(TEST_ALL), "--tests", str(tests), "--lock", str(lock), "--wait", "0"],
                           cwd=tmp_path, capture_output=True, text=True, timeout=120, env=nested)  # fmt: skip
        assert r.returncode == 0 and r.stdout.startswith("test_all: 1 passed")
    finally:
        holder.kill()
        holder.wait()
    code, out = script(TEST_ALL, tmp_path, "--tests", str(tests), "--lock", str(lock), "--wait", "0")
    assert code == 0 and out[0].startswith("test_all: 1 passed")
