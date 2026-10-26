"""The RunConfig JSON Schema from the registries (GET /schema/run-config), read only. Use it to write a run config file."""

from __future__ import annotations

import click
from jsonschema import Draft202012Validator

from simcli.client import CliError
from simcli.options import api, common, json_file
from simcli.output import col, emit, fields, table

CHOICES = {  # option -> ($defs entry, key with the params by choice, field with the enum)
    "policy": ("PolicySettings", "x-params-by-kind", "kind"),
    "tool": ("ToolGrant", "x-params-by-tool", "tool_id"),
    "indicator": ("IndicatorRef", "x-params-by-indicator", "id"),
}


def type_of(node: dict) -> str:
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in node:
        return " | ".join(type_of(n) for n in node["anyOf"])
    if "enum" in node:
        return "enum"
    if "const" in node:
        return f"const {node['const']}"
    kind = node.get("type", "any")
    if kind == "array":
        return f"{type_of(node.get('items', {}))}[]"
    return "null" if kind == "null" else str(kind)


def field_rows(node: dict) -> list[dict]:
    required = set(node.get("required", []))
    return [
        {"field": k, "type": type_of(v), "required": k in required, "research_locked": bool(v.get("x-research-locked"))}
        for k, v in node.get("properties", {}).items()
    ]


def enum_of(node: dict) -> list:
    if "enum" in node:
        return node["enum"]
    return next((enum_of(n) for n in node.get("anyOf", []) if enum_of(n)), [])


def locked_fields(schema: dict) -> list[str]:
    """Research-locked fields as `RunConfig.x` or `Def.x`."""
    out = [f"RunConfig.{k}" for k, v in schema.get("properties", {}).items() if v.get("x-research-locked")]
    for name, d in schema.get("$defs", {}).items():
        out += [f"{name}.{k}" for k, v in d.get("properties", {}).items() if v.get("x-research-locked")]
    return out


def choices(schema: dict) -> dict[str, list]:
    defs = schema.get("$defs", {})
    out = {"role": enum_of(defs.get("AgentConfig", {}).get("properties", {}).get("role", {}))}
    for option, (name, _, field) in CHOICES.items():
        out[option] = enum_of(defs.get(name, {}).get("properties", {}).get(field, {}))
    return out


def summary_text(schema: dict) -> str:
    columns = [col("field"), col("type"), col("required"), col("research_locked")]
    picks = "\n".join(f"{k:<10} {', '.join(map(str, v))}" for k, v in choices(schema).items())
    locked = "\n".join(locked_fields(schema))
    defs = ", ".join(schema.get("$defs", {}))
    return (
        f"{table(field_rows(schema), columns)}\n\nchoices\n{picks}\n\nresearch-locked (leave null in a research config)\n{locked}"
        f"\n\n$defs: {defs}\nMore: --def NAME, --policy KIND, --tool ID, --indicator ID."
    )


def part_text(node: dict) -> str:
    rows = field_rows(node)
    if not rows:
        return "(no params)" if node.get("type") == "object" or not node else type_of(node)
    return table(rows, [col("field"), col("type"), col("required"), col("research_locked")])


def pick(name: str, value: str, options: dict) -> dict:
    if value not in options:
        raise CliError("cli_usage", f"Unknown {name} {value!r}. Choices: {', '.join(options)}.", exit_code=2)
    return options[value]


def resolve(node: dict, schema: dict) -> dict:
    """Follow a $ref, and take the non-null branch of a nullable anyOf."""
    if "anyOf" in node:
        branches = [n for n in node["anyOf"] if n.get("type") != "null"]
        node = branches[0] if len(branches) == 1 else node
    if "$ref" in node:
        node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def nullable(node: dict) -> bool:
    kinds = node.get("type")
    return any(n.get("type") == "null" for n in node.get("anyOf", [])) or kinds == "null" or (isinstance(kinds, list) and "null" in kinds)


def locked_values(value, node: dict, schema: dict, path: str) -> list[dict]:
    """Research configs: a nullable research-locked field must be null (the server gives research_field_locked).
    A locked field with a non-null default (memory/summary) must equal the condition's value; the server checks that one."""
    node = resolve(node, schema)
    out = []
    if isinstance(value, dict):
        for key, child in value.items():
            prop = node.get("properties", {}).get(key)
            if prop is None:
                continue
            here = f"{path}/{key}" if path else key
            if prop.get("x-research-locked") and nullable(prop) and child is not None:
                out.append({"path": here, "message": "set by the condition in a research config: leave it null"})
            else:
                out += locked_values(child, prop, schema, here)
    elif isinstance(value, list) and "items" in node:
        for i, child in enumerate(value):
            out += locked_values(child, node["items"], schema, f"{path}/{i}")
    return out


def check(ctx, path: str, schema: dict) -> None:
    config = json_file(path, "config file")
    errors = [
        {"path": "/".join(map(str, e.absolute_path)) or "(root)", "message": e.message}
        for e in sorted(Draft202012Validator(schema).iter_errors(config), key=lambda e: list(map(str, e.absolute_path)))
    ]
    if not errors and isinstance(config, dict) and config.get("kind") == "research":
        errors = locked_values(config, schema, schema, "")
    if errors:
        raise CliError("config_invalid", f"{path}: {len(errors)} problem(s) against the server's RunConfig schema.", errors=errors)
    emit(ctx, {"ok": True, "path": path, "kind": config.get("kind"), "errors": []}, fields)


@click.group()
def schema():
    """JSON Schemas from the server's registries (read only)."""


@schema.command("run-config")
@click.option("--def", "def_name", help="Print one $defs entry, for example AgentConfig or LlmSettings.")
@click.option("--policy", help="Print the params schema of this policy kind.")
@click.option("--tool", help="Print the params schema of this tool id.")
@click.option("--indicator", help="Print the params schema of this indicator id.")
@click.option("--check", "check_file", help="Check a run config file against the schema, here. Sends no write.")
@common
@click.pass_context
def run_config(ctx, def_name, check_file, **by_choice):
    """The RunConfig JSON Schema (GET /schema/run-config): fields, registry choices, research-locked fields, params per choice."""
    given = {k: v for k, v in by_choice.items() if v is not None}
    if len(given) + (def_name is not None) + (check_file is not None) > 1:
        raise click.UsageError("Give at most one of --def, --policy, --tool, --indicator and --check.")
    body = api(ctx).get("schema/run-config")
    if check_file is not None:
        return check(ctx, check_file, body)
    if def_name is not None:
        return emit(ctx, pick("$defs entry", def_name, body.get("$defs", {})), part_text)
    if given:
        ((option, value),) = given.items()
        name, key, _ = CHOICES[option]
        return emit(ctx, pick(option, value, body.get("$defs", {}).get(name, {}).get(key, {})), part_text)
    emit(ctx, body, summary_text)


COMMANDS = (schema,)
