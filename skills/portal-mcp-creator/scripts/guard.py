"""Request guard for guided exploration. Pure logic, no browser needed.

Rules (see HANDOFF "Skillen portal-mcp-creator: pipeline", step 5):
- Only the service's own host is allowed.
- Before login: everything on the host is allowed, so the login can complete.
- After login: every non-GET request is blocked.
- After login: GET requests whose path looks like a side effect are blocked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Path segments that suggest a side effect even on GET. Matched case-insensitively
# against each segment, split on "/", "-", "_" and ".".
SIDE_EFFECT_WORDS = {
    "logout", "logoff", "signout", "sign-out", "log-out", "unsubscribe", "deregister",
    "avregistrera", "avregistrering", "delete", "remove", "destroy", "purge", "revoke",
    "reset", "archive", "cancel", "deactivate", "disable", "close", "markread", "mark-read",
    "read", "confirm", "approve", "accept", "reject", "submit", "send", "pay", "order",
    "create", "update", "edit", "save", "transfer", "book", "bid", "buy", "sell",
}

_SPLIT = re.compile(r"[/\-_.]+")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def _segments(path: str) -> list[str]:
    return [s for s in _SPLIT.split(path.lower()) if s]


def side_effect_hint(path: str) -> str | None:
    """Return the word that makes this path look like a side effect, or None."""
    for segment in _segments(path):
        if segment in SIDE_EFFECT_WORDS:
            return segment
    # Also catch joined forms such as "markasread" or "unsubscribeall".
    joined = path.lower().replace("/", "")
    for word in ("logout", "unsubscribe", "delete", "remove", "markasread", "markread", "cancel", "signout"):
        if word in joined:
            return word
    return None


def decide(method: str, url: str, service_host: str, logged_in: bool) -> Decision:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != service_host.lower():
        return Decision(False, f"outside the service domain ({host or 'no host'})")
    if not logged_in:
        return Decision(True, "before login")
    method = method.upper()
    if method != "GET":
        return Decision(False, f"{method} is blocked after login")
    hint = side_effect_hint(parts.path)
    if hint is not None:
        return Decision(False, f"GET path looks like a side effect (\"{hint}\")")
    return Decision(True, "GET after login")
