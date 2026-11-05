"""Field selection (`pick`) and removal (`redact`) on JSON responses.

Paths are dotted, for example "owner.national_id". When a value is a list, the
path is applied to every item in the list.
"""

from __future__ import annotations

from typing import Any


def _split(path: str) -> list[str]:
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise ValueError("empty field path")
    return parts


def redact(data: Any, paths: list[str]) -> Any:
    """Return a copy of data with every field at the given paths removed."""
    result = data
    for path in paths:
        result = _redact_one(result, _split(path))
    return result


def _redact_one(data: Any, parts: list[str]) -> Any:
    if isinstance(data, list):
        return [_redact_one(item, parts) for item in data]
    if not isinstance(data, dict):
        return data
    key, rest = parts[0], parts[1:]
    if key not in data:
        return data
    copy = dict(data)
    if not rest:
        del copy[key]
    else:
        copy[key] = _redact_one(copy[key], rest)
    return copy


def pick(data: Any, paths: list[str]) -> Any:
    """Keep only the fields at the given paths. Lists are filtered item by item."""
    if isinstance(data, list):
        return [pick(item, paths) for item in data]
    if not isinstance(data, dict):
        return data
    result: dict[str, Any] = {}
    for path in paths:
        _pick_into(data, result, _split(path))
    return result


def _pick_into(source: Any, target: dict[str, Any], parts: list[str]) -> None:
    if not isinstance(source, dict) or parts[0] not in source:
        return
    key, rest = parts[0], parts[1:]
    value = source[key]
    if not rest:
        target[key] = value
        return
    if isinstance(value, list):
        existing = target.get(key)
        items = existing if isinstance(existing, list) else [{} for _ in value]
        for index, item in enumerate(value):
            if index >= len(items):
                items.append({})
            if isinstance(items[index], dict):
                _pick_into(item, items[index], rest)
        target[key] = items
        return
    child = target.setdefault(key, {})
    if isinstance(child, dict):
        _pick_into(value, child, rest)
