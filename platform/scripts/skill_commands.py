"""Regenerate the `commands:` block of the collusion-sim-cli SKILL.md from the Click tree of `simcli.main.cli`.

    uv run python scripts/skill_commands.py

The block is the last key of the frontmatter. The script replaces it and leaves the other keys and the body unchanged.
tests/cli/test_skill.py checks that the file equals the tree, with this module's `click_tree`.
"""

from __future__ import annotations

from pathlib import Path

import click
import yaml

from simcli.main import cli

SKILL = Path(__file__).parents[1] / ".claude" / "skills" / "collusion-sim-cli" / "SKILL.md"
COMMON = {"--json", "--server", "--timeout", "--help"}


def entry(name: str, c: click.Command) -> dict:
    e = {"name": name, "description": (c.help or "").strip().splitlines()[0]}
    args = [p.name for p in c.params if isinstance(p, click.Argument)]
    opts = [max(p.opts, key=len) for p in c.params if isinstance(p, click.Option) and not set(p.opts) & COMMON]
    return {**e, **({"arguments": args} if args else {}), **({"options": opts} if opts else {})}


def click_tree() -> list[dict]:
    tree = []
    for name, c in cli.commands.items():
        if isinstance(c, click.Group):
            subs = [entry(n, s) for n, s in c.commands.items()]
            tree.append({"group": name, "description": c.help.strip().splitlines()[0], "subcommands": subs})
        else:
            e = entry(name, c)
            tree.append({"command": e.pop("name"), **e})
    return tree


def main() -> None:
    _, front, body = SKILL.read_text(encoding="utf-8").split("---\n", 2)
    head = front[: front.index("\ncommands:\n") + 1]
    block = yaml.safe_dump({"commands": click_tree()}, sort_keys=False, allow_unicode=True, width=100)
    SKILL.write_text(f"---\n{head}{block}---\n{body}", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
