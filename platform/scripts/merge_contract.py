"""Resolve contract merge conflicts during a `git merge --no-commit`.

Takes the branch's `info.version` line as the given version, puts the branch's change-log entry above the HEAD
entries, and turns its "- next (" line into the version. Run in the main tree:

    uv run --no-project python -I scripts/merge_contract.py <branch> <version>
"""

import re
import sys

OPENAPI = "docs/sim/api/openapi.yaml"
README = "docs/sim/api/README.md"


def main(branch: str, version: str) -> int:
    conflict = re.compile(r"<<<<<<< HEAD\n(.*?)=======\n(.*?)>>>>>>> " + re.escape(branch) + r"\n", re.S)

    text = open(OPENAPI, encoding="utf-8").read()
    for m in conflict.finditer(text):
        if "version:" not in m.group(1):
            print(f"{OPENAPI}: a conflict outside the version line; resolve it by hand")
            return 1
    text = conflict.sub(f"  version: {version}\n", text)
    text = re.sub(r"^  version: \d+\.\d+\.\d+$", "  version: " + version, text, count=1, flags=re.M)
    open(OPENAPI, "w", encoding="utf-8", newline="\n").write(text)

    text = conflict.sub(lambda m: m.group(2) + m.group(1), open(README, encoding="utf-8").read())
    if text.count("- next (") != 1:
        print(f'{README}: expected one "- next (" entry, found {text.count("- next (")}')
        return 1
    text = re.sub(r"- next \((\d+ Oct)[^)]*\)", lambda m: f"- {version} ({m.group(1)}, version set by the lead)", text, count=1)
    open(README, "w", encoding="utf-8", newline="\n").write(text)

    for path in (OPENAPI, README):
        if "<<<<<<<" in open(path, encoding="utf-8").read():
            print(f"{path}: conflict markers remain")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
