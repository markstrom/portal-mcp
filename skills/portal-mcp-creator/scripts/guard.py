"""Request guard for guided exploration. Pure logic, no browser needed.

Rules:
- Only the service's own host is allowed, plus login hosts that the manifest declares
  (identity providers, BankID gateways, account services) while the user logs in.
- A login host is allowed only before login and only over https. After login, every
  request to it is blocked. A login host never counts as the service's host.
- A data host (an API the service's pages load data from) is allowed for GET over https.
  After login, non-GET requests to it are blocked and captured like the service's own,
  unless the user explicitly agreed to a real action.
- An asset host (a CDN with a library or stylesheet the pages need) is allowed only for GET
  over https of static files: scripts, stylesheets, fonts and images. Never data requests,
  navigation or writes.
- With login_hosts_after_login "oidc", a login host stays open after login only for the
  standard OpenID Connect endpoints that its discovery document names (same host): GET for
  discovery, keys, authorization, userinfo and session check; POST only to the token endpoint.
  The end-session (logout) endpoint is never opened.
- Any other host is blocked as "not declared".
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

ASSET_TYPES = {"script", "stylesheet", "font", "image"}
DISCOVERY_PATHS = ("/.well-known/openid-configuration", "/.well-known/jwks.json")
_OIDC_GET_KEYS = ("authorization_endpoint", "userinfo_endpoint", "jwks_uri", "check_session_iframe")
_OIDC_POST_KEYS = ("token_endpoint",)


def oidc_endpoints_from(discovery: dict, served_by: str) -> dict[str, set[str]]:
    """Paths a login host may serve after login, taken from its discovery document.

    Only endpoints on the host that served the document are used. The end-session endpoint
    (logout) is never included.
    """
    served_by = served_by.lower()
    allowed = {"get": set(DISCOVERY_PATHS), "post": set()}
    for keys, method in ((_OIDC_GET_KEYS, "get"), (_OIDC_POST_KEYS, "post")):
        for key in keys:
            value = discovery.get(key) if isinstance(discovery, dict) else None
            if not isinstance(value, str):
                continue
            parts = urlsplit(value)
            if parts.scheme == "https" and (parts.hostname or "").lower() == served_by and parts.path:
                allowed[method].add(parts.path)
    return allowed


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
    data_hosts: tuple[str, ...] | list[str] = (),
    *,
    asset_hosts: tuple[str, ...] | list[str] = (),
    resource_type: str | None = None,
    login_after_login: str = "blocked",
    oidc_endpoints: dict[str, dict[str, set[str]]] | None = None,
) -> Decision:
    """allow_writes lets non-GET requests through. Use it only after the user agreed to a real action.

    login_hosts and data_hosts are the hosts declared in the manifest.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != service_host.lower():
        if host and host in {h.lower() for h in data_hosts}:
            if parts.scheme != "https":
                return Decision(False, f"data host {host} needs https")
            if method.upper() == "GET":
                return Decision(True, f"data host {host}: GET")
            if logged_in and allow_writes:
                return Decision(True, f"{method.upper()} to data host {host} allowed: the user agreed to a real action")
            return Decision(False, f"{method.upper()} to data host {host} is blocked" + (" (captured)" if logged_in else ""))
        if host and host in {h.lower() for h in asset_hosts}:
            if parts.scheme != "https":
                return Decision(False, f"asset host {host} needs https")
            if method.upper() != "GET":
                return Decision(False, f"asset host {host}: GET only")
            if resource_type not in ASSET_TYPES:
                return Decision(
                    False,
                    f"asset host {host}: only scripts, styles, fonts and images (this was {resource_type or 'unknown'})",
                )
            return Decision(True, f"asset host {host}: {resource_type}")
        if host and host in {h.lower() for h in login_hosts}:
            if logged_in and login_after_login == "oidc" and parts.scheme == "https":
                allowed = (oidc_endpoints or {}).get(host) or {"get": set(DISCOVERY_PATHS), "post": set()}
                verb = method.upper()
                if verb == "GET" and (parts.path in allowed["get"] or parts.path in DISCOVERY_PATHS):
                    return Decision(True, f"login host {host}: OpenID Connect endpoint after login")
                if verb == "POST" and parts.path in allowed["post"]:
                    return Decision(True, f"login host {host}: token endpoint after login")
                return Decision(
                    False,
                    f"login host {host} after login: only the OpenID Connect endpoints named by its discovery document",
                )
            if logged_in:
                return Decision(False, f"login host {host} is blocked after login")
            if parts.scheme != "https":
                return Decision(False, f"login host {host} needs https")
            # Login pages post their own forms and BankID flows poll with POST, so all methods
            # are allowed here before login, as on the service's own host before login.
            return Decision(True, f"login host {host} during login")
        return Decision(
            False,
            f"host {host or '(none)'} is not declared: not the service's host, and not in login_hosts or data_hosts",
        )
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


def should_capture(
    method: str, url: str, service_host: str, logged_in: bool, data_hosts: tuple[str, ...] | list[str] = ()
) -> bool:
    """A blocked request is captured as a write only after login, only for non-GET, and only
    for the service's own host or a declared data host. Login hosts and others never are."""
    host = (urlsplit(url).hostname or "").lower()
    allowed = {service_host.lower(), *(h.lower() for h in data_hosts)}
    return logged_in and method.upper() != "GET" and host in allowed
