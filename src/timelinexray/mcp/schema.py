"""A strict validator for the JSON Schema subset the MCP tools use, plus schema fragments.

Tool input and output schemas are published to clients as JSON Schema 2020-12 documents
(the MCP default dialect). The server validates every tool call's arguments against the
tool's ``inputSchema`` with :func:`validate` before any handler runs, and the test suite
validates every tool result against its ``outputSchema`` with the same function.

Supported keywords: ``type`` (a name or a list of names), ``enum``, ``const``,
``pattern`` (unanchored search as in JSON Schema; ``$`` matches only at the very end
of the string, as in ECMA-262, never before a trailing newline), ``minLength`` /
``maxLength`` (Unicode code points), ``minimum`` / ``maximum``, ``items``, ``minItems`` /
``maxItems``, ``properties``, ``required``, ``additionalProperties`` (``false`` or a
schema), ``anyOf``, and local references: ``$defs`` (only at the root of a schema
document) with ``$ref`` of the form ``#/$defs/<name>`` (a ``$ref`` object holds nothing
else; every reference must resolve, and a definition may not refer to itself without
structure in between). Annotation keywords (``description``, ``title``, ``default``,
``$schema``, ``examples``) are ignored. Any other keyword makes :func:`check_schema` fail,
so a schema can never silently validate less than it appears to.

Differences from general JSON Schema, chosen for strictness: ``integer`` accepts only
JSON integers (not ``1.0``) and never booleans; ``number`` never accepts booleans.

Published output schemas
------------------------
:func:`published_output` turns a tool's strict output schema into the lean schema listed by
``tools/list``: descriptions are dropped, the envelope and the ``data`` object stay exact
(closed, every member required, all constraints), and deeper levels keep only their shape
(member names, JSON types, ``enum`` and ``const``) down to the deepest level that fits a
per-tool byte budget; below that only the JSON type is given. Repeated shapes become
``$defs`` entries. Every published schema is therefore a relaxation of the strict one: a
value valid under the strict schema is valid under the published one. The server validates
every tool result against the strict schema before it is sent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

DIALECT = "https://json-schema.org/draft/2020-12/schema"

_TYPES: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}

_ANNOTATIONS = frozenset({"description", "title", "default", "$schema", "examples"})
_KEYWORDS = frozenset(
    {
        "type", "enum", "const", "pattern", "minLength", "maxLength", "minimum", "maximum",
        "items", "minItems", "maxItems", "properties", "required", "additionalProperties",
        "anyOf", "$ref",
    }
) | _ANNOTATIONS
_REF_PREFIX = "#/$defs/"
_DEF_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")

_PATTERNS: dict[str, re.Pattern[str]] = {}


class SchemaViolation(ValueError):
    """A value does not match a schema; ``where`` is a JSON-pointer-like location."""

    def __init__(self, where: str, message: str) -> None:
        super().__init__(f"{where or 'value'}: {message}")
        self.where = where
        self.detail = message


def _pattern(text: str) -> re.Pattern[str]:
    compiled = _PATTERNS.get(text)
    if compiled is None:
        if "\\$" in text:  # pragma: no cover - the published schemas use $ only as an anchor
            raise ValueError(f"escaped '$' is not supported in patterns: {text!r}")
        # Python's "$" also matches before a final newline; ECMA-262's does not.
        compiled = _PATTERNS[text] = re.compile(text.replace("$", r"\Z"))
    return compiled


def _describe(value: Any) -> str:
    for name in ("boolean", "integer", "number", "string", "array", "object", "null"):
        if _TYPES[name](value):
            return name
    return type(value).__name__  # pragma: no cover - JSON values only


def _resolve(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    """Follow ``$ref`` (``#/$defs/<name>`` of ``root``) until a schema with structure."""
    seen: set[str] = set()
    while "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            raise ValueError(f"reference cycle at {ref!r}")
        seen.add(ref)
        name = ref[len(_REF_PREFIX):] if isinstance(ref, str) and ref.startswith(_REF_PREFIX) else None
        defs = root.get("$defs", {})
        if name is None or name not in defs:
            raise ValueError(f"unresolvable reference {ref!r}")
        schema = defs[name]
    return schema


def validate(value: Any, schema: Mapping[str, Any], where: str = "",
             root: Mapping[str, Any] | None = None) -> None:
    """Raise :class:`SchemaViolation` unless ``value`` matches ``schema``.

    ``root`` is the schema document that ``$ref`` values point into; it defaults to
    ``schema`` itself (the call for a whole document).
    """
    root = schema if root is None else root
    if "$ref" in schema:
        schema = _resolve(schema, root)
    if "anyOf" in schema:
        errors = []
        for option in schema["anyOf"]:
            try:
                validate(value, option, where, root)
                break
            except SchemaViolation as exc:
                errors.append(exc.detail)
        else:
            raise SchemaViolation(where, "matches none of the allowed forms: " + "; ".join(errors))
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[name](value) for name in names):
            raise SchemaViolation(where, f"expected {' or '.join(names)}, got {_describe(value)}")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise SchemaViolation(where, f"must be {schema['const']!r}")
    if "enum" in schema and not any(
        value == option and type(value) is type(option) for option in schema["enum"]
    ):
        allowed = ", ".join(repr(option) for option in schema["enum"])
        raise SchemaViolation(where, f"must be one of {allowed}; got {value!r}"[:600])
    if isinstance(value, str):
        _check_string(value, schema, where)
    elif _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaViolation(where, f"must be >= {schema['minimum']}, got {value}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaViolation(where, f"must be <= {schema['maximum']}, got {value}")
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaViolation(where, f"needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaViolation(where, f"allows at most {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(item, schema["items"], f"{where}/{index}", root)
    elif isinstance(value, dict):
        _check_object(value, schema, where, root)


def _check_string(value: str, schema: Mapping[str, Any], where: str) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        raise SchemaViolation(where, f"must have at least {schema['minLength']} characters")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise SchemaViolation(
            where, f"must have at most {schema['maxLength']} characters, got {len(value)}"
        )
    if "pattern" in schema and not _pattern(schema["pattern"]).search(value):
        raise SchemaViolation(where, f"does not match the pattern {schema['pattern']}")


def _check_object(value: dict[str, Any], schema: Mapping[str, Any], where: str,
                  root: Mapping[str, Any]) -> None:
    properties: Mapping[str, Any] = schema.get("properties", {})
    for name in schema.get("required", ()):
        if name not in value:
            raise SchemaViolation(f"{where}/{name}", "is required")
    extra = schema.get("additionalProperties", True)
    for name, item in value.items():
        if name in properties:
            validate(item, properties[name], f"{where}/{name}", root)
        elif extra is False:
            known = ", ".join(sorted(properties)) or "none"
            raise SchemaViolation(
                f"{where}/{name}"[:200], f"unknown property (allowed: {known})"
            )
        elif isinstance(extra, Mapping):
            validate(item, extra, f"{where}/{name}", root)


def check_schema(schema: Any, where: str = "", root: Mapping[str, Any] | None = None) -> None:
    """Fail on keywords outside the supported subset (see the module documentation)."""
    if not isinstance(schema, Mapping):
        raise ValueError(f"{where or 'schema'}: a schema must be an object")
    if root is None:
        root = schema
        defs = schema.get("$defs", {})
        if not isinstance(defs, Mapping):
            raise ValueError(f"{where or 'schema'}: $defs must be an object")
        for name, sub in defs.items():
            if not isinstance(name, str) or not _DEF_NAME.fullmatch(name):
                raise ValueError(f"{where or 'schema'}: invalid $defs name {name!r}")
            check_schema(sub, f"{where}/$defs/{name}", root)
        schema = {key: value for key, value in schema.items() if key != "$defs"}
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise ValueError(f"{where or 'schema'}: unsupported keywords {sorted(unknown)}")
    if "$ref" in schema:
        if set(schema) - _ANNOTATIONS - {"$ref"}:
            raise ValueError(f"{where or 'schema'}: a $ref object may hold nothing else")
        _resolve(schema, root)  # raises on an unresolvable reference or a cycle
        return
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        for name in names:
            if name not in _TYPES:
                raise ValueError(f"{where or 'schema'}: unknown type {name!r}")
    if "pattern" in schema:
        _pattern(schema["pattern"])
    for name, sub in schema.get("properties", {}).items():
        check_schema(sub, f"{where}/properties/{name}", root)
    if isinstance(schema.get("additionalProperties"), Mapping):
        check_schema(schema["additionalProperties"], f"{where}/additionalProperties", root)
    if "items" in schema:
        check_schema(schema["items"], f"{where}/items", root)
    for index, sub in enumerate(schema.get("anyOf", ())):
        check_schema(sub, f"{where}/anyOf/{index}", root)


# -- fragments ---------------------------------------------------------------------------

#: ``content_trust`` value of every citation: the cited bytes are upstream data.
UNTRUSTED = "UNTRUSTED_SOURCE_DATA"

COMMIT_FULL = {
    "type": "string",
    "pattern": "^[0-9a-f]{40}$",
    "description": "Full 40-digit commit id of a pinned commit (use resolve_commit for a prefix).",
}
SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
OID = {"type": "string", "pattern": "^[0-9a-f]{40}$|^[0-9a-f]{64}$"}
LINE = {"type": "integer", "minimum": 1, "maximum": 16_777_215}


def string(max_length: int, *, min_length: int = 1, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "minLength": min_length, "maxLength": max_length, **extra}


def nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    result = {**schema, "type": [*kinds, "null"]}
    if "enum" in schema:
        result["enum"] = [*schema["enum"], None]
    return result


def obj(
    properties: Mapping[str, Any],
    required: Sequence[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A closed object; every property is required unless ``required`` says otherwise."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(properties) if required is None else list(required),
        **extra,
    }


def array(items: Mapping[str, Any], max_items: int, **extra: Any) -> dict[str, Any]:
    return {"type": "array", "items": dict(items), "maxItems": max_items, **extra}


def counts(max_items: int = 64) -> dict[str, Any]:
    """An object mapping names to non-negative integers."""
    return {
        "type": "object",
        "additionalProperties": {"type": "integer", "minimum": 0},
        "description": f"name -> count (at most {max_items} names)",
    }


# -- published (lean) output schemas -------------------------------------------------------

#: Keywords kept below the exact levels: the shape of a value, not its constraints.
_SHAPE = frozenset({"type", "enum", "const", "properties", "items", "additionalProperties",
                    "anyOf", "$ref"})
#: Byte budget of one published output schema (compact JSON), see :func:`published_output`.
OUTPUT_SCHEMA_BUDGET = 2200
_FACTOR_MIN_BYTES = 60


def _compact(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _kinds(schema: Mapping[str, Any]) -> list[str]:
    kind = schema.get("type")
    return kind if isinstance(kind, list) else [kind] if isinstance(kind, str) else []


def project(schema: Mapping[str, Any], *, exact_depth: int, shape_depth: int,
            depth: int = 0) -> dict[str, Any]:
    """A relaxation of ``schema``: exact above ``exact_depth``, shape only below it.

    ``depth`` counts object levels from the root (the envelope is 0, its members 1, the
    members of ``data`` 2, ...). Objects and arrays at ``shape_depth`` or deeper keep only
    their JSON type. Annotations are dropped everywhere. Every value valid under ``schema``
    is valid under the result.
    """
    kinds = _kinds(schema)
    if depth >= shape_depth and ("object" in kinds or "array" in kinds):
        return {"type": schema["type"]}
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _ANNOTATIONS or key == "$defs":
            continue
        if depth >= exact_depth and key not in _SHAPE:
            continue
        if key == "additionalProperties" and value is False and depth >= exact_depth:
            continue
        if key == "properties":
            out[key] = {name: project(sub, exact_depth=exact_depth, shape_depth=shape_depth,
                                      depth=depth + 1) for name, sub in value.items()}
        elif key == "items":
            out[key] = project(value, exact_depth=exact_depth, shape_depth=shape_depth,
                               depth=depth)
        elif key == "additionalProperties" and isinstance(value, Mapping):
            out[key] = project(value, exact_depth=exact_depth, shape_depth=shape_depth,
                               depth=depth + 1)
        elif key == "anyOf":
            out[key] = [project(sub, exact_depth=exact_depth, shape_depth=shape_depth,
                                depth=depth) for sub in value]
        else:
            out[key] = value
    return out


def factor(schema: dict[str, Any], min_bytes: int = _FACTOR_MIN_BYTES) -> dict[str, Any]:
    """Move sub-schemas that occur more than once into ``$defs`` (deterministic names).

    The largest saving is factored first; a definition is named after the property under
    which it first occurs (``citation``, ``citation_2``, ...). The result validates exactly
    the same values as the input.
    """
    import json

    defs: dict[str, Any] = {}
    names: dict[str, str] = {}

    def candidates(node: Any, key: str, found: dict[str, list[str]], top: bool) -> None:
        if not isinstance(node, dict):
            return
        if not top and "$ref" not in node:
            text = _compact(node)
            if len(text) >= min_bytes:
                found.setdefault(text, []).append(key)
        for name, sub in node.get("properties", {}).items():
            candidates(sub, name, found, False)
        for member in ("items", "additionalProperties"):
            if isinstance(node.get(member), dict):
                candidates(node[member], key, found, False)
        for sub in node.get("anyOf", ()):
            candidates(sub, key, found, False)

    def replace(node: Any, text: str, ref: dict[str, str]) -> Any:
        if isinstance(node, dict):
            if _compact(node) == text:
                return dict(ref)
            return {k: replace(v, text, ref) for k, v in node.items()}
        if isinstance(node, list):
            return [replace(item, text, ref) for item in node]
        return node

    while True:
        found: dict[str, list[str]] = {}
        candidates(schema, "", found, True)
        for definition in defs.values():
            candidates(definition, "", found, True)
        best = None
        for text, keys in sorted(found.items()):
            if len(keys) < 2:
                continue
            saving = (len(keys) - 1) * len(text) - len(keys) * 24 - 16
            if saving > 0 and (best is None or saving > best[0]):
                best = (saving, text, keys[0])
        if best is None:
            break
        _, text, key = best
        base = re.sub(r"[^A-Za-z0-9_]", "_", key) or "def"
        base = base if base[0].isalpha() else f"d_{base}"
        name, number = base[:56], 1
        while name in defs:
            number += 1
            name = f"{base[:56]}_{number}"
        ref = {"$ref": _REF_PREFIX + name}
        schema = replace(schema, text, ref)
        defs = {k: replace(v, text, ref) for k, v in defs.items()}
        defs[name] = json.loads(text)
        names[text] = name
    if defs:
        schema = {**schema, "$defs": dict(sorted(defs.items()))}
    return schema


def published_output(strict: Mapping[str, Any], *,
                     budget: int = OUTPUT_SCHEMA_BUDGET) -> dict[str, Any]:
    """The lean output schema ``tools/list`` publishes for the strict ``strict``.

    The envelope (depth 0) and its members, including the ``data`` object (depth 1), stay
    exact; members of ``data`` and below keep their shape. The shape is kept to the deepest
    level whose factored projection fits ``budget`` bytes, and always at least to the
    members of ``data`` (their names, types and enums); deeper objects and arrays give only
    their type.
    """
    best: dict[str, Any] | None = None
    for shape_depth in range(2, 12):
        candidate = factor(project(strict, exact_depth=2, shape_depth=shape_depth))
        if best is not None and len(_compact(candidate)) > budget:
            break
        best = candidate
        if _compact(project(strict, exact_depth=2, shape_depth=shape_depth + 1)) == \
                _compact(project(strict, exact_depth=2, shape_depth=shape_depth)):
            break
    assert best is not None
    return best
