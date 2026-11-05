"""Field selection (`pick`) and removal (`redact`) on JSON responses.

Paths are dotted, for example "owner.national_id". When a value is a list, the
path is applied to every item in the list.

A pick path may list fallbacks separated by "|", for example "item.name|name": the first
path that exists in the item is used, and its value is written at the first path, so
items of different shapes come out in one shape.
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


def alternatives(path: str) -> list[list[str]]:
    """The dotted paths of a pick path, first one first: "a.b|c" -> [["a", "b"], ["c"]]."""
    return [_split(alt.strip()) for alt in path.split("|")]


def pick(data: Any, paths: list[str]) -> Any:
    """Keep only the fields at the given paths. Lists are filtered item by item."""
    if isinstance(data, list):
        return [pick(item, paths) for item in data]
    if not isinstance(data, dict):
        return data
    result: dict[str, Any] = {}
    for path in paths:
        alts = alternatives(path)
        if len(alts) == 1:
            _pick_into(data, result, alts[0])
            continue
        _pick_first(data, result, alts)
    return result


_MISSING = object()


def _get(source: Any, parts: list[str]) -> Any:
    """The value at a dotted path through objects, or _MISSING. A list on the way is
    reported as such (the caller falls back to item-by-item picking)."""
    value = source
    for index, part in enumerate(parts):
        if isinstance(value, list):
            return ("list", index)
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _pick_first(source: dict[str, Any], target: dict[str, Any], alts: list[list[str]]) -> None:
    """Use the first alternative that exists; write its value at the first alternative's path."""
    canonical = alts[0]
    for alt in alts:
        value = _get(source, alt)
        if value is _MISSING:
            continue
        if isinstance(value, tuple) and value and value[0] == "list":
            # A list inside the path: the alternatives that share the path up to the list
            # are tried again for each item.
            at = value[1]
            prefix = alt[:at]
            rests = [a[at:] for a in alts if a[:at] == prefix and len(a) > at]
            items = source
            for part in prefix:
                items = items[part]
            node = target
            for part in prefix[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            existing = node.get(prefix[-1])
            picked = existing if isinstance(existing, list) and len(existing) == len(items) else [{} for _ in items]
            for index, item in enumerate(items):
                if isinstance(item, dict) and isinstance(picked[index], dict):
                    _pick_first(item, picked[index], rests)
            node[prefix[-1]] = picked
            return
        node = target
        for part in canonical[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[canonical[-1]] = value
        return


def has_content(value: Any) -> bool:
    """Does an answer hold any data? Empty objects, lists, strings and nulls do not."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, dict):
        return any(has_content(v) for v in value.values())
    if isinstance(value, list):
        return any(has_content(v) for v in value)
    return True


def unmatched_paths(result: Any, paths: list[str]) -> list[str]:
    """Pick paths that found nothing in any item of an answer that went through pick."""
    return [path for path in paths if not _found(result, alternatives(path)[0])]


def _found(value: Any, parts: list[str]) -> bool:
    if isinstance(value, list):
        return any(_found(item, parts) for item in value)
    if not parts:
        return value is not None
    if not isinstance(value, dict) or parts[0] not in value:
        return False
    return _found(value[parts[0]], parts[1:])


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
