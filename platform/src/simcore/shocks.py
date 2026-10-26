"""Generic parameter shocks: a JSON pointer into the demand params, an op and a value."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from simschema.base import Frozen


def _tokens(path: str) -> list[str]:
    return [t.replace("~1", "/").replace("~0", "~") for t in path.split("/")[1:]]


class Shock(Frozen):
    path: str = Field(pattern=r"^(/[^/]+)+$", description="JSON pointer into the demand params, for example /a, /c or /c_offset/A")
    op: Literal["add", "mul", "set"]
    value: float

    def apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """New params with this shock applied. A missing key inside an existing map counts as 0 (an offset not yet set)."""
        *parents, leaf = _tokens(self.path)
        out = dict(params)
        node = out
        for key in parents:
            if not isinstance(node.get(key), dict):
                raise ValueError(f"{self.path}: {key} is not an object in the params")
            node[key] = dict(node[key])
            node = node[key]
        old = node.get(leaf, 0.0)
        if not isinstance(old, (int, float)):
            raise ValueError(f"{self.path} is not a number in the params")
        node[leaf] = {"add": old + self.value, "mul": old * self.value, "set": self.value}[self.op]
        return out

    def direction(self, params: dict[str, Any]) -> int:
        """Sign of the change this shock makes from the given params: 1 up, −1 down, 0 none."""
        new = self.apply(params)
        node_old, node_new = params, new
        for key in _tokens(self.path):
            node_old, node_new = (node_old.get(key, 0.0) if isinstance(node_old, dict) else 0.0), node_new[key]
        diff = node_new - node_old
        return (diff > 0) - (diff < 0)


def path_in_schema(schema: dict, path: str) -> bool:
    """True when the pointer names a number field of the JSON schema (properties, or additionalProperties maps)."""
    defs = schema.get("$defs", {})
    node = schema
    for key in _tokens(path):
        while "$ref" in node:
            node = defs[node["$ref"].rsplit("/", 1)[-1]]
        if key in node.get("properties", {}):
            node = node["properties"][key]
        elif isinstance(node.get("additionalProperties"), dict):
            node = node["additionalProperties"]
        else:
            return False
    return node.get("type") in ("number", "integer")
