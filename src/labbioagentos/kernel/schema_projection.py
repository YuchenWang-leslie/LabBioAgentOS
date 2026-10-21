"""Provider-side JSON Schema projection; local validation stays authoritative."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Keywords whose value is one subschema.
_SCHEMA_KEYWORDS = frozenset({
    "additionalItems", "additionalProperties", "contains", "else", "if", "items",
    "not", "propertyNames", "then", "unevaluatedItems", "unevaluatedProperties",
})
# Keywords whose value is a list of subschemas.
_SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
# Keywords whose value maps names to subschemas.
_SCHEMA_MAP_KEYWORDS = frozenset({
    "$defs", "definitions", "dependentSchemas", "patternProperties", "properties",
})


def project_json_schema(
    schema: Mapping[str, Any], *, removed_keywords: frozenset[str]
) -> dict[str, Any]:
    """Copy ``schema`` without ``removed_keywords`` at any schema position.

    Property names and data under keywords such as ``enum``, ``const`` and
    ``default`` are never altered. The projection may only widen what the
    provider enforces; the host still validates the full schema locally.
    """

    projected = _project(schema, removed_keywords)
    if not isinstance(projected, dict):
        raise ValueError("A JSON Schema projection requires an object schema")
    return projected


def _project(schema: Any, removed: frozenset[str]) -> Any:
    if not isinstance(schema, Mapping):
        # Boolean schemas and malformed positions are copied unchanged.
        return _copy(schema)
    projected = {}
    for keyword, value in schema.items():
        if keyword in removed:
            continue
        if keyword in _SCHEMA_KEYWORDS:
            projected[keyword] = (
                [_project(item, removed) for item in value]
                if isinstance(value, list) else _project(value, removed)
            )
        elif keyword in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            projected[keyword] = [_project(item, removed) for item in value]
        elif keyword in _SCHEMA_MAP_KEYWORDS and isinstance(value, Mapping):
            projected[keyword] = {
                name: _project(item, removed) for name, item in value.items()
            }
        else:
            projected[keyword] = _copy(value)
    return projected


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value
