"""The runtime's login checks the session with a copy of the service host's cookies,
from its own short-lived client, so nothing the service sets reaches the browser profile."""

from __future__ import annotations

import httpx
import yaml

from portal_mcp.login.local import _capture
from portal_mcp.manifest import Manifest


class _Context:
    """A browser context. It has no `request` attribute: the check must not go through it."""

    def __init__(self, cookies):
        self._cookies = cookies
        self.cookie_urls: list[list[str]] = []

    async def cookies(self, urls):
        self.cookie_urls.append(list(urls))
        return self._cookies


class _Page:
    def __init__(self, storage=None):
        self.storage = storage or {}

    async def evaluate(self, script):
        if "navigator.userAgent" in script:
            return "TestBrowser/1.0"
        for key, value in self.storage.items():
            if repr(key) in script:
                return value
        return None


def _manifest(extra: str = "") -> Manifest:
    return Manifest.model_validate(yaml.safe_load(f"""
name: example
base_url: https://app.example.com
login_url: https://login.example.com/start
login_hosts: [login.example.com]
session: {{check: /api/session{extra}}}
"""))


def _transport(status: int, seen: list[httpx.Request], set_cookie: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {"set-cookie": set_cookie} if set_cookie else {}
        if 300 <= status < 400:
            headers["location"] = "https://login.example.com/authorize"
        return httpx.Response(status, headers=headers)

    return httpx.MockTransport(handler)


SID = {"name": "sid", "value": "s1", "domain": "app.example.com", "path": "/"}


async def test_check_uses_only_the_service_hosts_cookies_and_its_own_client():
    seen: list[httpx.Request] = []
    context = _Context([SID])
    session = await _capture(context, _Page(), _manifest(), transport=_transport(200, seen))
    assert session is not None and session.cookies[0]["name"] == "sid"
    assert context.cookie_urls == [["https://app.example.com"]]
    (request,) = seen
    assert str(request.url) == "https://app.example.com/api/session"
    assert request.headers["cookie"] == "sid=s1"
    assert request.headers["user-agent"] == "TestBrowser/1.0"


async def test_redirecting_check_is_not_logged_in_and_its_cookies_are_dropped():
    seen: list[httpx.Request] = []
    context = _Context([SID])
    result = await _capture(
        context, _Page(), _manifest(), transport=_transport(302, seen, set_cookie="nonce.abc=n; Path=/")
    )
    assert result is None
    # the context has no way to receive cookies from the check: only cookies() was called on it
    assert context.cookie_urls == [["https://app.example.com"]]


async def test_bearer_services_send_the_token_with_the_check():
    manifest = Manifest.model_validate(yaml.safe_load("""
name: example
base_url: https://app.example.com
login_url: https://app.example.com/login
session: {type: bearer, bearer_source: "localStorage:token", check: /api/me}
"""))
    seen: list[httpx.Request] = []
    session = await _capture(_Context([]), _Page({"token": "t-123"}), manifest, transport=_transport(200, seen))
    assert session is not None and session.bearer == "t-123"
    assert seen[0].headers["authorization"] == "Bearer t-123"


async def test_bearer_without_token_does_not_call_the_service():
    manifest = Manifest.model_validate(yaml.safe_load("""
name: example
base_url: https://app.example.com
login_url: https://app.example.com/login
session: {type: bearer, bearer_source: "localStorage:token", check: /api/me}
"""))
    seen: list[httpx.Request] = []
    assert await _capture(_Context([]), _Page(), manifest, transport=_transport(200, seen)) is None
    assert seen == []
