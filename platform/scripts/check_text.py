"""Text check before a commit (DECISIONS-13 §3): no CRLF and no BOM in a tracked or staged text file, then ruff check and
ruff format --check on the Python code. Prints each problem and exits 1, or exits 0 when all is clean.

    uv run python scripts/check_text.py [--no-ruff]

Run it from the repo root (or any folder in the repo). It reads the index, so a staged file is checked as it will be
committed, and the working tree, so an unstaged CRLF shows too. Git's own binary test decides what is text.

Limits (CR-59 m3, m4):
- Run it after `git add`. A new file is checked only once it is in the index. Git converts CRLF at `add`, but not a BOM.
- Commit messages are not checked (a BOM there is the committer's to avoid). Submodule entries (mode 160000) are skipped.
- It reads every text blob of the index in one `git cat-file --batch` call (about 10 MB today) and keeps three bytes of each.
- Ruff checks src, tests and scripts only. analysis/, figures/, site/tools/ and docs/ scripts are not in its scope.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"
CRLF = {"crlf", "mixed"}  # git ls-files --eol values that hold a CRLF
RUFF_PATHS = ("src", "tests", "scripts")
GITLINK = b"160000"  # a submodule entry: no blob to read


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=root, input=data, capture_output=True, check=True).stdout


def entries(root: Path) -> list[tuple[str, str, str, str]]:
    """(path, blob id, index eol, worktree eol) of each index entry; eol is lf, crlf, mixed, none, -text or '' (no file)."""
    blobs = {}
    for line in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if line:
            meta, path = line.split(b"\t", 1)
            mode, blob = meta.split()[:2]
            if mode != GITLINK:
                blobs[path.decode()] = blob.decode()
    out = []
    for line in git(root, "ls-files", "--eol", "-z").split(b"\0"):
        if line:
            meta, path = line.split(b"\t", 1)
            i, w = (m.decode().split("/", 1)[1] for m in meta.split()[:2])
            if path.decode() in blobs:
                out.append((path.decode(), blobs[path.decode()], i, w))
    return out


def index_heads(root: Path, blob_ids: list[str]) -> dict[str, bytes]:
    """First three bytes of each blob. `git cat-file --batch` prints each whole blob; only three bytes of each are kept."""
    raw = git(root, "cat-file", "--batch", data="".join(f"{b}\n" for b in blob_ids).encode())
    heads, pos = {}, 0
    while pos < len(raw):
        end = raw.index(b"\n", pos)
        sha, _, size = raw[pos:end].split()
        heads[sha.decode()] = raw[end + 1 : end + 4]
        pos = end + 1 + int(size) + 1
    return heads


def text_problems(root: Path) -> list[str]:
    rows = [r for r in entries(root) if "-text" not in (r[2], r[3])]
    heads = index_heads(root, sorted({r[1] for r in rows}))
    problems = []
    for path, blob, i, w in rows:
        if i in CRLF:
            problems.append(f"{path}: CRLF in the index")
        elif w in CRLF:
            problems.append(f"{path}: CRLF in the working tree")
        if heads[blob].startswith(BOM):
            problems.append(f"{path}: BOM in the index")
        else:
            file = root / path
            if file.is_file():
                with file.open("rb") as f:
                    if f.read(3) == BOM:
                        problems.append(f"{path}: BOM in the working tree")
    return problems


def ruff_problems(root: Path) -> list[str]:
    paths = [p for p in RUFF_PATHS if (root / p).is_dir()]
    if not paths:
        return []
    problems = []
    for step in (["check", "--quiet"], ["format", "--check", "--quiet"]):
        r = subprocess.run([sys.executable, "-m", "ruff", *step, *paths], cwd=root, capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode:
            problems += [f"ruff {step[0]}: {line}" for line in (r.stdout + r.stderr).splitlines() if line.strip()]
    return problems


def main(argv: list[str]) -> int:
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, encoding="utf-8", errors="replace", check=True
        ).stdout.strip()
    )
    problems = text_problems(root) + ([] if "--no-ruff" in argv else ruff_problems(root))
    for p in problems:
        print(p)
    print(f"check_text: {len(problems)} problem(s)" if problems else "check_text: clean")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
