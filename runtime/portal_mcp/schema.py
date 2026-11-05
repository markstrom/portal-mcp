"""Response structure (field names and JSON types, never values) and comparison of two structures.

Used by the creator skill's analysis and by self_test, which compares what a read tool
returns now with the structure saved when the tool was verified.
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 6
MAX_LIST_ITEMS = 200  # items looked at for variants
MAX_VARIANTS = 10
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
        if not isinstance(value[0], dict):
            return [schema_of(value[0], depth + 1)]
        # Objects in a list may come in more than one shape: one variant per set of keys.
        variants: list[Any] = []
        seen: set[frozenset] = set()
        for item in value[:MAX_LIST_ITEMS]:
            if not isinstance(item, dict):
                continue
            keys = frozenset(item)
            if keys in seen:
                continue
            seen.add(keys)
            variants.append(schema_of(item, depth + 1))
            if len(variants) >= MAX_VARIANTS:
                break
        return variants
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
        if len(old) == 1 and len(new) == 1:
            _compare(old[0], new[0], f"{path}[]", out)
            return
        # Item variants: each saved variant is compared with the current variant most like it.
        used: set[int] = set()
        for variant in old:
            best = max(range(len(new)), key=lambda i: _overlap(variant, new[i]))
            used.add(best)
            _compare(variant, new[best], f"{path}[]", out)
        for index, variant in enumerate(new):
            if index not in used:
                keys = sorted(variant) if isinstance(variant, dict) else [_kind(variant)]
                out["added"].append(f"{path}[] variant {keys}")
        for label_list in out.values():  # one entry per difference, even when variants repeat it
            label_list[:] = list(dict.fromkeys(label_list))
        return
    if _kind(old) != _kind(new):
        # integer and number are close enough for JSON
        if {_kind(old), _kind(new)} == {"integer", "number"}:
            return
        out["changed"].append(f"{label}: {_kind(old)} -> {_kind(new)}")


def _overlap(a: Any, b: Any) -> int:
    if isinstance(a, dict) and isinstance(b, dict):
        return len(set(a) & set(b)) * 2 - len(set(a) ^ set(b))
    return 1 if _kind(a) == _kind(b) else 0


def has_fields(schema: Any) -> bool:
    """Does a structure describe any data? [{}], {}, empty lists and nulls do not."""
    if isinstance(schema, dict):
        return any(has_fields(v) for v in schema.values())
    if isinstance(schema, list):
        return any(has_fields(v) for v in schema)
    return schema not in (EMPTY_LIST, NULL)


def is_breaking(diff: dict[str, list[str]]) -> bool:
    return bool(diff["removed"] or diff["changed"])
