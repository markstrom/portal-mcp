"""A text-free skeleton of an HTML page, for writing `response: html` tools.

The skeleton holds structure only: JSON embedded in <script> tags as field names and
types, and repeated element patterns as tags, classes, attribute names and counts. No
text, no attribute values and no JSON values. The page is parsed as data; no script runs.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from portal_mcp.schema import schema_of

MAX_PATTERNS = 25
MAX_CHILDREN = 10
MAX_EMBEDDED = 10
MAX_SCHEMA_CHARS = 20000
MIN_REPEATS = 3
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,60}$")
# Keys that are probably data rather than field names: ids, e-mail addresses, phrases.
DATA_LIKE_KEY = re.compile(r"\d{3,}|@|\s|:")


def _safe(names: list[str]) -> list[str]:
    return [n for n in names if SAFE_NAME.match(n)]


def _signature(node) -> str:
    classes = _safe((node.attributes.get("class") or "").split())[:3]
    return node.tag + "".join(f".{c}" for c in classes)


def _mask_keys(schema: Any) -> Any:
    """Replace object keys that look like values (ids, e-mails, phrases) with <key>."""
    if isinstance(schema, dict):
        out: dict = {}
        for key, sub in schema.items():
            name = "<key>" if DATA_LIKE_KEY.search(key) else key
            if name in out:
                continue  # one example of a keyed map is enough
            out[name] = _mask_keys(sub)
        return out
    if isinstance(schema, list):
        return [_mask_keys(item) for item in schema]
    return schema


def _script_selector(node) -> str | None:
    node_id = node.attributes.get("id")
    if node_id and SAFE_NAME.match(node_id):
        return f"script#{node_id}"
    kind = (node.attributes.get("type") or "").lower()
    if kind in ("application/json", "application/ld+json"):
        return f'script[type="{kind}"]'
    return None


def embedded(tree) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for node in tree.css("script"):
        selector = _script_selector(node)
        kind = (node.attributes.get("type") or "").lower()
        if selector is None or ("json" not in kind and not node.attributes.get("id")):
            continue
        try:
            data = json.loads(node.text(deep=True) or "")
        except ValueError:
            continue
        if selector in seen:
            continue
        seen.add(selector)
        schema = _mask_keys(schema_of(data))
        text = json.dumps(schema)
        found.append({
            "select": selector,
            "schema": schema if len(text) <= MAX_SCHEMA_CHARS else "too large to show; narrow it with path",
            "top_level_keys": sorted(_mask_keys({k: 0 for k in data}).keys()) if isinstance(data, dict) else None,
        })
        if len(found) >= MAX_EMBEDDED:
            break
    return found


def patterns(tree) -> list[dict]:
    """Repeated elements with children, such as result rows or cards."""
    counts: Counter = Counter()
    nodes: dict[str, list] = defaultdict(list)
    for node in tree.root.traverse() if tree.root is not None else []:
        if not node.is_element_node or node.tag in ("html", "head", "body", "script", "style"):
            continue
        signature = _signature(node)
        if "." not in signature:
            continue  # bare tags repeat everywhere; classes make a pattern
        counts[signature] += 1
        if len(nodes[signature]) < 20:
            nodes[signature].append(node)
    out = []
    for signature, count in counts.items():
        if count < MIN_REPEATS:
            continue
        children: Counter = Counter()
        attrs: set[str] = set()
        for node in nodes[signature]:
            for child in node.traverse():
                if child is node or not child.is_element_node:
                    continue
                children[_signature(child)] += 1
                attrs.update(a for a in child.attributes if SAFE_NAME.match(a) and a not in ("class", "style"))
        if not children:
            continue
        sample = len(nodes[signature])
        out.append({
            "select": signature,
            "count": count,
            "children": {sig: round(n / sample, 1) for sig, n in children.most_common(MAX_CHILDREN)},
            "attribute_names": sorted(attrs)[:20],
        })
    out.sort(key=lambda p: (-p["count"] * min(len(p["children"]), 5), p["select"]))
    return out[:MAX_PATTERNS]


def skeleton(html: str) -> dict:
    tree = LexborHTMLParser(html)
    found = embedded(tree)
    tree.strip_tags(["script", "style", "noscript", "template"])
    return {"embedded_json": found, "repeated_elements": patterns(tree)}
