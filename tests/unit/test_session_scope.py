"""The runtime's login captures cookies for the service's own host only, never for login hosts."""

from __future__ import annotations

import yaml

from portal_mcp.login.local import _capture
from portal_mcp.manifest import Manifest


class _Response:
    status = 200


class _Request:
    def __init__(self):
        self.urls: list[str] = []

    async def get(self, url, max_redirects=0):
        self.urls.append(url)
        return _Response()


class _Context:
    def __init__(self):
        self.request = _Request()
        self.cookie_urls: list[list[str]] = []

    async def cookies(self, urls):
        self.cookie_urls.append(list(urls))
        return [{"name": "sid", "value": "s", "domain": "app.example.com", "path": "/"}]


async def test_session_check_and_cookies_use_only_the_service_host():
    manifest = Manifest.model_validate(yaml.safe_load("""
name: example
base_url: https://app.example.com
login_url: https://login.example.com/start
login_hosts: [login.example.com]
session: {check: /api/session}
"""))
    context = _Context()
    session = await _capture(context, page=None, manifest=manifest)
    assert session is not None
    assert context.request.urls == ["https://app.example.com/api/session"]
    assert context.cookie_urls == [["https://app.example.com"]]
