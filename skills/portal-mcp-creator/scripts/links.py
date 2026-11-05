"""Menu links found on a page after a verified login. Pure logic, no browser needed.

Only links to the service's own host that look like reading are kept. Logout, delete,
unsubscribe and similar links are dropped, and so are file downloads.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import side_effect_hint  # noqa: E402

MAX_LINKS = 200
FILE_SUFFIXES = (
    ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ics", ".txt", ".xml", ".json",
)
# Link texts that mean an action, in English and Swedish.
ACTION_TEXT = re.compile(
    r"\b(log ?out|log ?off|sign ?out|logga ut|unsubscribe|avregistrera|avsluta|delete|remove|"
    r"radera|ta bort|cancel|avbryt|säg upp|pay|betala|send|skicka|submit)\b",
    re.IGNORECASE,
)

# Collects anchors in the page; run with page.evaluate(COLLECT_JS).
COLLECT_JS = """() => {
  const inMenu = (a) => !!a.closest('nav, header, [role="navigation"], [role="menu"], [role="menubar"], aside');
  return Array.from(document.querySelectorAll('a[href]')).map(a => ({
    href: a.href,
    text: (a.innerText || a.getAttribute('aria-label') || a.title || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
    in_menu: inMenu(a),
  }));
}"""


class NotLoggedIn(RuntimeError):
    pass


def filter_menu_links(raw: list[dict], service_host: str, *, logged_in: bool) -> dict:
    """Return {"links": [...], "dropped": {...}}. Refuses to run before a verified login."""
    if not logged_in:
        raise NotLoggedIn("menu links are collected only after the login is verified")
    service_host = service_host.lower()
    seen: set[str] = set()
    links: list[dict] = []
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for item in raw:
        href = str(item.get("href", ""))
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https"):
            drop("not a web link")
            continue
        if (parts.hostname or "").lower() != service_host:
            drop("other host")
            continue
        path = parts.path or "/"
        hint = side_effect_hint(path)
        text = str(item.get("text", ""))
        if hint is not None or ACTION_TEXT.search(text):
            drop("looks like an action")
            continue
        if path.lower().endswith(FILE_SUFFIXES):
            drop("file download")
            continue
        key = path + (f"?{parts.query}" if parts.query else "")
        if key in seen:
            continue
        seen.add(key)
        links.append({"path": key, "text": text, "in_menu": bool(item.get("in_menu"))})

    links.sort(key=lambda link: (not link["in_menu"], link["path"]))
    if len(links) > MAX_LINKS:
        dropped["over the limit"] = len(links) - MAX_LINKS
        links = links[:MAX_LINKS]
    return {"links": links, "dropped": dropped}


def valid_visit(path: object, service_host: str) -> str | None:
    """A path the user chose to visit: relative, same host, not a side effect. Else None."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    if side_effect_hint(urlsplit(path).path) is not None:
        return None
    return path
