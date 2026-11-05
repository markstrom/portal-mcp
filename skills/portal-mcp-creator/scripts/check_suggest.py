"""Candidate session checks seen in a recording. Pure logic, no browser needed.

After a verified login, the service's own pages load JSON endpoints. Those that answered 200
are candidates for `session.check`; the recorder then tests them logged in and logged out.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import side_effect_hint  # noqa: E402

MAX_SUGGESTIONS = 8
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-fA-F]{16,}|[A-Za-z0-9_-]{24,})$")
_FILE_SUFFIXES = (".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ico", ".pdf")


def candidate_from(method: str, url: str, status: int, content_type: str | None,
                   service_host: str, current_check: str) -> str | None:
    """The path (with query) of a request that could be a session check, or None."""
    if method.upper() != "GET" or status != 200:
        return None
    if "json" not in (content_type or "").lower():
        return None
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != service_host.lower():
        return None
    path = parts.path or "/"
    if path.lower().endswith(_FILE_SUFFIXES) or side_effect_hint(path) is not None:
        return None
    candidate = path + (f"?{parts.query}" if parts.query else "")
    if candidate == current_check or path == current_check:
        return None
    return candidate


def _id_segments(path: str) -> int:
    return sum(1 for segment in urlsplit(path).path.split("/") if segment and _ID_SEGMENT.match(segment))


def rank(candidates: list[str]) -> list[str]:
    """Unique candidates, best first: no query, fewer ID-like segments, shorter."""
    unique = list(dict.fromkeys(candidates))
    unique.sort(key=lambda c: ("?" in c, _id_segments(c), len(urlsplit(c).path.split("/")), len(c), c))
    return unique[:MAX_SUGGESTIONS]
