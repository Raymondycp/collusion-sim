"""Role registry. An agent is role + policy + capability; the role sets the ceiling of what any agent of it may do."""

from __future__ import annotations

from typing import Literal

from simschema.base import Frozen
from simschema.plugins import load_all

Phase = Literal["news", "push", "pull", "talk", "act", "settle"]
# The run loop runs the phases in this order. In each phase it runs every agent whose role lists the phase:
# the host first, then sellers in seed order, then other agents in config order.
PHASES: tuple[Phase, ...] = ("news", "push", "pull", "talk", "act", "settle")


class RoleSpec(Frozen):
    """`phases`: the loop phases an agent of this role takes part in. `output_events`: the only event types the run may
    record while an agent of this role is acting (the run checks it)."""

    name: str
    stage: Literal["run", "evaluation"]
    phases: list[Phase]
    tools: list[str]
    output_events: list[str]
    research_allowed: bool
    research_scripted_only: bool = False
    built: bool
    notes: str = ""


class RoleRegistry:
    def __init__(self) -> None:
        self._roles: dict[str, RoleSpec] = {}

    def register(self, role: RoleSpec) -> RoleSpec:
        if role.name in self._roles:
            raise ValueError(f"role {role.name} is already registered")
        self._roles[role.name] = role
        return role

    def __getitem__(self, name: str) -> RoleSpec:
        return self._roles[name]

    def __contains__(self, name: str) -> bool:
        return name in self._roles

    def specs(self) -> list[RoleSpec]:
        return list(self._roles.values())


ROLES = RoleRegistry()
load_all("simcore.builtin.roles")
