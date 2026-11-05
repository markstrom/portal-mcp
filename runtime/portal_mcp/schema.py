"""Response structure (field names and JSON types, never values) and comparison of two structures.

Used by the creator skill's analysis and by self_test, which compares what a read tool
returns now with the structure saved when the tool was verified.
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 6
EMPTY_LIST = "empty"
NULL = "null"


def schema_of(value: Any, depth: int = 0) -> Any:
    """Structure only: field names and JSON types, never values."""
    if depth > MAX_DEPTH:
        return "..."
    if isinstance(value, dict):
        return {k: schema_of(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return [EMPTY_LIST]
        return [schema_of(value[0], depth + 1)]
    if value is None:
        return NULL
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _kind(schema: Any) -> str:
    if isinstance(schema, dict):
        return "object"
    if isinstance(schema, list):
        return "array"
    return str(schema)


def compare(baseline: Any, current: Any, path: str = "") -> dict[str, list[str]]:
    """Differences between a saved structure and the current one.

    removed and changed usually break a tool; added and nullable are informational.
    An empty list or a null value says nothing about the type, so it is not a change.
    """
    out: dict[str, list[str]] = {"removed": [], "added": [], "changed": [], "nullable": []}
    _compare(baseline, current, path, out)
    return out


def _compare(old: Any, new: Any, path: str, out: dict[str, list[str]]) -> None:
    label = path or "(root)"
    if old == "..." or new == "...":
        return
    if old == NULL or new == NULL:
        if old != new:
            out["nullable"].append(label)
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in old:
            child = f"{path}.{key}" if path else key
            if key not in new:
                out["removed"].append(child)
            else:
                _compare(old[key], new[key], child, out)
        for key in new:
            if key not in old:
                out["added"].append(f"{path}.{key}" if path else key)
        return
    if isinstance(old, list) and isinstance(new, list):
        if not old or not new or old[0] == EMPTY_LIST or new[0] == EMPTY_LIST:
            return
        _compare(old[0], new[0], f"{path}[]", out)
        return
    if _kind(old) != _kind(new):
        # integer and number are close enough for JSON
        if {_kind(old), _kind(new)} == {"integer", "number"}:
            return
        out["changed"].append(f"{label}: {_kind(old)} -> {_kind(new)}")


def is_breaking(diff: dict[str, list[str]]) -> bool:
    return bool(diff["removed"] or diff["changed"])
