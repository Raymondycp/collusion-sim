"""Copy conditions/ for a test as it was before the 24 Oct freeze: draft condition files and no research files.

The repo's conditions/ now holds the frozen research inputs (status frozen, frozen.json, models.yaml, judge-rubric.yaml).
Tests build their own freeze state, so each copy starts from drafts."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ("frozen.json", "models.yaml", "judge-rubric.yaml", "decision-rule.yaml")


def draft_conditions(dst: Path, *ignore: str) -> Path:
    shutil.copytree(ROOT / "conditions", dst, ignore=shutil.ignore_patterns(*RESEARCH, *ignore))
    for f in dst.glob("*.yaml"):
        text = f.read_text(encoding="utf-8")
        new = re.sub(r"(?m)^status: frozen$", "status: draft", re.sub(r'(?m)^frozen_hash: "sha256:[0-9a-f]+"$', "frozen_hash: null", text))
        if new != text:
            f.write_text(new, encoding="utf-8", newline="\n")
    return dst
