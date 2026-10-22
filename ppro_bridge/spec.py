"""The action spec (actions.json): what every panel action takes and returns, and which CLI command calls what.

One file is the contract for the panel (plugin/lib/spec.js is generated from it), the fake panel and the CLI.
`check_params` is the param check the fake panel and the tests use; the panel runs the same rules in JS.

Rules (a JSON-Schema subset): `type` is one of string, number, integer, boolean, array, object, any; `required`;
`enum`; `minimum` / `maximum`; `minItems`; `items` (array element schema); `properties` (object schema — unknown
keys are refused unless `additionalProperties` is true); `pairedWith` (both or neither). A param sent as null
counts as absent. `default` is documentation: handlers apply defaults. At action level, `requireOneOf` lists name
groups of which at least one must be present (non-empty when it is an array). Unknown top-level params are refused.
"""

import json
import math
from functools import lru_cache
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().with_name("actions.json")


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _actions() -> dict:
    return {a["name"]: a for a in load()["actions"]}


@lru_cache(maxsize=1)
def _commands() -> dict:
    return {c["name"]: c for c in load()["commands"]}


def action(name: str) -> dict | None:
    return _actions().get(name)


def action_names() -> list[str]:
    return [a["name"] for a in load()["actions"]]


def command(name: str) -> dict | None:
    return _commands().get(name)


def command_names() -> list[str]:
    return [c["name"] for c in load()["commands"]]


def protocol() -> int:
    return load()["protocol"]


def kind(name: str) -> str | None:
    a = action(name)
    return a["kind"] if a else None


def timeout(name: str) -> int:
    a = action(name)
    return a["timeoutSeconds"] if a else load()["defaultTimeoutSeconds"]


def _type_ok(kind_: str, value) -> bool:
    if kind_ == "any":
        return True
    if kind_ == "string":
        return isinstance(value, str)
    if kind_ == "boolean":
        return isinstance(value, bool)
    if kind_ in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False
        return kind_ == "number" or float(value).is_integer()
    if kind_ == "array":
        return isinstance(value, list)
    if kind_ == "object":
        return isinstance(value, dict)
    raise ValueError(f"spec: unknown type {kind_!r}")


def check_value(schema: dict, value, path: str) -> list[str]:
    """Problems with one value against its schema (empty list = fine)."""
    t = schema.get("type", "any")
    if not _type_ok(t, value):
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    problems = []
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: must be one of {', '.join(map(str, schema['enum']))}")
    if t in ("number", "integer"):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: must be <= {schema['maximum']}")
    if t == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, v in enumerate(value):
                problems += check_value(schema["items"], v, f"{path}[{i}]")
    if t == "object" and "properties" in schema:
        problems += check_object(schema["properties"], value, path,
                                 additional=schema.get("additionalProperties", False))
    return problems


def check_object(properties: dict, obj: dict, path: str, *, additional=False, require_one_of=()) -> list[str]:
    problems = []
    if not additional:
        problems += [f"{path}.{k}: unknown param" for k in obj if k not in properties]
    for name, schema in properties.items():
        value = obj.get(name)
        if value is None:
            if schema.get("required"):
                problems.append(f"{path}.{name}: required")
            continue
        problems += check_value(schema, value, f"{path}.{name}")
        partner = schema.get("pairedWith")
        if partner and obj.get(partner) is None:
            problems.append(f"{path}.{name}: needs {partner} too")
    for group in require_one_of:
        if not any(obj.get(n) not in (None, [], "") for n in group):
            problems.append(f"{path}: needs one of {', '.join(group)}")
    return problems


def check_params(action_name: str, params: dict | None) -> list[str]:
    """Problems with a request's params for one action; an unknown action is one problem."""
    a = action(action_name)
    if a is None:
        return [f"unknown action {action_name!r}"]
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return ["params: expected object"]
    return check_object(a["params"], params, "params", require_one_of=a.get("requireOneOf", ()))
