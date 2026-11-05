"""Turn an HTML page into JSON for read tools with `response: html`.

The page is parsed as data with selectolax: no script runs and nothing else is fetched.
Only element text, attribute values and JSON that the page embeds in a <script> tag are
returned, never raw HTML.
"""

from __future__ import annotations

import json
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from .manifest import EmbeddedJson, ExtractField

MAX_HTML_BYTES = 10 * 1024 * 1024
# Elements whose content is never text that a person reads.
NON_TEXT_TAGS = ["script", "style", "noscript", "template"]


class ExtractError(ValueError):
    pass


def _parse(html: str, max_bytes: int) -> LexborHTMLParser:
    if len(html.encode("utf-8", errors="ignore")) > max_bytes:
        raise ExtractError(f"the page is larger than {max_bytes} bytes")
    return LexborHTMLParser(html)


def _text(node) -> str:
    # Text nodes are joined as they are, so words split by a removed tag stay whole.
    return " ".join(node.text(deep=True, separator="", strip=False).split())


def _value(node, field: ExtractField) -> Any:
    if field.fields is not None:
        return {name: _field(node, sub) for name, sub in field.fields.items()}
    if field.attr is not None:
        value = node.attributes.get(field.attr)
        return value.strip() if isinstance(value, str) else None
    return _text(node)


def _field(scope, field: ExtractField) -> Any:
    many = field.many if field.many is not None else field.fields is not None
    if many:
        return [_value(node, field) for node in scope.css(field.select)]
    node = scope.css_first(field.select)
    return None if node is None else _value(node, field)


def extract(html: str, recipe: dict[str, ExtractField], *, max_bytes: int = MAX_HTML_BYTES) -> dict[str, Any]:
    """Fields named in the recipe, from CSS selectors. A selector that matches nothing gives
    null (or an empty list), so a changed page shows up as changed structure, not an error."""
    tree = _parse(html, max_bytes)
    tree.strip_tags(NON_TEXT_TAGS)
    root = tree.root
    if root is None:
        return {name: ([] if (f.many if f.many is not None else f.fields is not None) else None)
                for name, f in recipe.items()}
    return {name: _field(root, field) for name, field in recipe.items()}


def _dig(value: Any, path: str | None) -> Any:
    if not path:
        return value
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def embedded_json(html: str, spec: EmbeddedJson, *, max_bytes: int = MAX_HTML_BYTES) -> Any:
    """JSON from the page's <script> tags matched by spec.select, narrowed by spec.path.
    Tags whose content is not valid JSON are skipped."""
    tree = _parse(html, max_bytes)
    found = []
    for node in tree.css(spec.select):
        if node.tag != "script":
            continue
        try:
            found.append(_dig(json.loads(node.text(deep=True) or ""), spec.path))
        except ValueError:
            continue
        if not spec.many:
            break
    if spec.many:
        return found
    return found[0] if found else None
