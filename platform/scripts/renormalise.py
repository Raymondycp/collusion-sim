"""One-time line-ending renormalisation (DECISIONS-13 §3, §6.7). Stages the change; it does not commit.

    uv run python scripts/renormalise.py

Run it from the repo root of a tree with no change to a tracked file: `git add --renormalize .` would stage that change
too, so the script stops when there is one. Untracked files are not touched. Steps:
1. `git add --renormalize .` (.gitattributes: every text file LF in the index).
2. Print each file that changed. Stop if one changed in more than its line ends (HEAD blob with CRLF made LF must equal
   the staged blob); the index is reset first, so nothing stays staged.
3. Write each changed file again from the index with `git checkout-index --force` (a plain checkout skips them: their
   stat data says they match). A file that `git diff --quiet` shows as edited since step 1 is not written; the script
   names it and exits 1, and its index entry stays renormalised (CR-59 addendum 3, r2).
   A git step that fails after step 1 prints git's error and "run `git reset -q` and start again", and exits 1.
4. scripts/check_text.py: no CRLF in the index or the working tree, ruff clean.
"""

from __future__ import annotations

import subprocess
import sys


def git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], input=data, capture_output=True, check=True).stdout


def unchanged(path: str) -> bool:
    """The working copy equals the index after the clean filter, so a CRLF copy of the same text counts as unchanged."""
    return subprocess.run(["git", "diff", "--quiet", "--", path]).returncode == 0


def main() -> int:
    if prefix := git("rev-parse", "--show-prefix").decode().strip():
        print(f"renormalise: run it from the repo root, not from {prefix}", file=sys.stderr)
        return 1
    edits = [line for line in git("status", "--porcelain", "--untracked-files=no").decode().splitlines() if line]
    if edits:
        print("renormalise: commit or park these changes first:", *edits, sep="\n  ", file=sys.stderr)
        return 1
    git("add", "--renormalize", ".")
    try:
        return staged()
    except subprocess.CalledProcessError as e:  # CR-59 addendum 4 n1: a git step failed with the index already staged
        print(f"renormalise: {' '.join(map(str, e.cmd))} failed:\n{e.stderr.decode(errors='replace')}", file=sys.stderr)
        print("renormalise: run `git reset -q` and start again", file=sys.stderr)
        return 1


def staged() -> int:
    changed = [p for p in git("diff", "--cached", "--name-only", "-z").decode().split("\0") if p]
    for p in changed:
        if git("show", f"HEAD:{p}").replace(b"\r\n", b"\n") != git("show", f":{p}"):
            git("reset", "-q")
            print(f"renormalise: {p} changed in more than its line ends; index reset, nothing written", file=sys.stderr)
            return 1
        print(p)
    edited = [p for p in changed if not unchanged(p)]
    rewrite = [p for p in changed if p not in edited]
    if rewrite:
        paths = "\0".join(rewrite).encode() + b"\0"
        # checkout-index --force also skips a file whose stat data matches; the same entries entered again carry none
        mine = set(rewrite)
        entries = [e for e in git("ls-files", "-s", "-z").split(b"\0") if e and e.split(b"\t", 1)[1].decode() in mine]
        git("update-index", "-z", "--index-info", data=b"\0".join(entries) + b"\0")
        git("checkout-index", "--force", "-z", "--stdin", data=paths)
    print(f"renormalise: {len(changed)} file(s) staged, line ends only")
    if edited:
        print("renormalise: edited during the run, not written (index renormalised):", *edited, sep="\n  ", file=sys.stderr)
        return 1
    return subprocess.run([sys.executable, "scripts/check_text.py"]).returncode


if __name__ == "__main__":
    sys.exit(main())
