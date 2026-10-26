"""GET /schema/run-config: the RunConfig JSON Schema (2020-12), with the registries written in as enums and per-item schemas.

Pydantic gives the shape; this module adds what only the registries know: the roles that can join a run, the policy kinds
(and each scripted policy's params), the tools (and each tool's grant params) and the indicators (and their params).
Fields that a condition or the seed map sets carry `x-research-locked: true`. No key field exists in RunConfig, so none
can appear here: an LLM agent holds only `provider_profile_id`.
"""

from __future__ import annotations

import copy
from typing import Any

import indicators
from services.configs import RunConfig
from services.research import CONDITION_FIELDS
from simcore.roles import ROLES
from simcore.tools import TOOLS

LOCKED = {"x-research-locked": True}


def run_config_schema(policy_types: list[dict]) -> dict[str, Any]:
    s = RunConfig.model_json_schema(ref_template="#/$defs/{model}")
    defs = s["$defs"]
    s = {"$schema": "https://json-schema.org/draft/2020-12/schema", **s}

    for f in CONDITION_FIELDS:
        s["properties"][f].update(LOCKED)
    defs["AgentConfig"]["properties"]["capability"].update(LOCKED)
    for f in ("system_prompt", "talk_prompt"):
        defs["LlmSettings"]["properties"][f].update(LOCKED)
    defs["MemorySettings"]["properties"]["summary"].update(
        LOCKED | {"description": "A research run uses the condition's memory_summary: leave it null or set the same value."}
    )

    run_roles = [r.name for r in ROLES.specs() if r.stage == "run" and r.built]
    defs["AgentConfig"]["properties"]["role"] = {"enum": run_roles, "title": "Role", "description": "A role from GET /roles"}

    defs["PolicySettings"]["properties"]["kind"] = {"enum": [p["kind"] for p in policy_types], "title": "Policy"}
    defs["PolicySettings"]["properties"]["params"]["description"] = "The params of the chosen kind (see x-params-by-kind)."
    defs["PolicySettings"]["x-params-by-kind"] = {p["kind"]: _params_object(p["params"]) for p in policy_types}

    specs = TOOLS.specs()
    grant = defs["ToolGrant"]
    grant["properties"]["tool_id"] = {"enum": [t.id for t in specs], "title": "Tool"}
    grant["x-params-by-tool"] = {t.id: copy.deepcopy(t.params_schema) for t in specs}

    ind = indicators.REGISTRY.specs()
    ref = defs["IndicatorRef"]
    ref["properties"]["id"] = {"enum": [i["id"] for i in ind], "title": "Indicator"}
    ref["x-params-by-indicator"] = {i["id"]: copy.deepcopy(i["params_schema"]) for i in ind}
    return s


def _params_object(params: list[dict]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            p["name"]: {**p.get("schema", {"type": p["type"]}), **({"default": p["default"]} if "default" in p else {})} for p in params
        },
    }
