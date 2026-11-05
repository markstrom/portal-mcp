"""Request guard for guided exploration. Pure logic, no browser needed.

Rules:
- Only the service's own host is allowed, plus login hosts that the manifest declares
  (identity providers, BankID gateways, account services) while the user logs in.
- A login host is allowed only before login and only over https. After login, every
  request to it is blocked. A login host never counts as the service's host.
- Before login: everything on the host is allowed, so the login can complete.
- After login: every non-GET request is blocked (captured, not sent), unless the user
  explicitly agreed to a real action for this recording.
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


def decide(
    method: str,
    url: str,
    service_host: str,
    logged_in: bool,
    allow_writes: bool = False,
    login_hosts: tuple[str, ...] | list[str] = (),
) -> Decision:
    """allow_writes lets non-GET requests through. Use it only after the user agreed to a real action.

    login_hosts are the hosts declared in the manifest's `login_hosts`.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != service_host.lower():
        if host and host in {h.lower() for h in login_hosts}:
            if logged_in:
                return Decision(False, f"login host {host} is blocked after login")
            if parts.scheme != "https":
                return Decision(False, f"login host {host} needs https")
            # Login pages post their own forms and BankID flows poll with POST, so all methods
            # are allowed here before login, as on the service's own host before login.
            return Decision(True, f"login host {host} during login")
        return Decision(False, f"outside the service domain ({host or 'no host'})")
    if not logged_in:
        return Decision(True, "before login")
    method = method.upper()
    if method != "GET":
        if allow_writes:
            return Decision(True, f"{method} allowed: the user agreed to a real action")
        return Decision(False, f"{method} is blocked after login")
    hint = side_effect_hint(parts.path)
    if hint is not None:
        return Decision(False, f"GET path looks like a side effect (\"{hint}\")")
    return Decision(True, "GET after login")


def should_capture(method: str, url: str, service_host: str, logged_in: bool) -> bool:
    """A blocked request is captured as a write only after login, only for non-GET, and only
    for the service's own host. Login hosts and other hosts are never captured."""
    host = (urlsplit(url).hostname or "").lower()
    return logged_in and method.upper() != "GET" and host == service_host.lower()
