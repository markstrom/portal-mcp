"""Public sites (session.type none): no login, no session, nothing stored."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from portal_mcp.login.base import LoginError, LoginManager
from portal_mcp.manifest import Manifest
from portal_mcp.service import ServiceClient, ServiceError

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import recon  # noqa: E402


def public(**extra) -> dict:
    data = {"name": "opensite", "base_url": "https://open.example.com", "session": {"type": "none"},
            "tools": [{"name": "info", "kind": "read", "description": "Info.", "method": "GET", "path": "/api/info"}]}
    data.update(extra)
    return data


def test_a_public_manifest_needs_no_login_url_or_check():
    manifest = Manifest.model_validate(public())
    assert manifest.needs_login is False
    assert manifest.login_url is None and manifest.session.check is None


def test_a_public_manifest_takes_no_login_settings():
    with pytest.raises(ValidationError, match="login hosts"):
        Manifest.model_validate(public(login_hosts=["login.example.com"]))
    with pytest.raises(ValidationError, match="login hosts"):
        Manifest.model_validate(public(login_hosts_after_login="oidc"))
    with pytest.raises(ValidationError, match="takes no check"):
        Manifest.model_validate(public(session={"type": "none", "check": "/api/session"}))


def test_a_login_manifest_still_needs_its_check_and_login_url():
    base = public(session={"type": "cookies", "check": "/api/session"})
    with pytest.raises(ValidationError, match="login_url is required"):
        Manifest.model_validate(base)
    with pytest.raises(ValidationError, match="session.check is required"):
        Manifest.model_validate({**base, "login_url": "https://open.example.com/", "session": {"type": "cookies"}})
    with pytest.raises(ValidationError):
        Manifest.model_validate({k: v for k, v in base.items() if k != "session"})  # session stays required


class NoLogins:
    def ensure(self, *args, **kwargs):
        raise AssertionError("a public service must never start a login")

    def pending(self):
        return None


async def test_a_public_service_opens_no_login_and_sends_no_session(tmp_path, monkeypatch):
    import httpx

    client = ServiceClient(Manifest.model_validate(public()), store=None, logins=NoLogins(), downloads=tmp_path)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"open": True})
        return httpx.Response(401)

    def fake_http(session):
        assert session is None
        return httpx.AsyncClient(base_url=client.manifest.base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(client, "_http", fake_http)
    assert await client.call("info", {}) == {"open": True}
    assert "cookie" not in seen[0].headers and "authorization" not in seen[0].headers
    assert client.session_status() == {"default": "no login needed"}
    assert "nothing to log out of" in await client.logout()

    # A 401 is an error, not a reason to log in.
    client.manifest.tools[0].path = "/api/private"
    with pytest.raises(ServiceError, match="without login"):
        await client.call("info", {})


async def test_the_login_manager_refuses_a_public_service():
    manager = LoginManager(provider=None, store=None)
    with pytest.raises(LoginError, match="no login"):
        await manager.ensure(Manifest.model_validate(public()), "default")


def test_the_analyzer_accepts_a_public_recording(tmp_path):
    (tmp_path / "recording.json").write_text(json.dumps({"login_verified": False, "public": True}))
    assert analyze._login_state(tmp_path / "recording.har") is True
    (tmp_path / "recording.json").write_text(json.dumps({"login_verified": False}))
    assert analyze._login_state(tmp_path / "recording.har") is False


def test_recon_tells_public_content_from_a_login():
    links = "".join(f'<a href="/p{i}">Page {i}</a>' for i in range(6))
    both = recon.page_profile(links + '<a href="https://id.example.com/login">Logga in</a>', 200, "https://example.com")
    assert both["public_content"] is True and both["login_found"] is True
    assert both["login_links"][0] == {"host": "id.example.com", "path": "/login", "text": "Logga in"}

    only_public = recon.page_profile(links, 200, "https://example.com")
    assert only_public["public_content"] is True and only_public["login_found"] is False

    login_page = recon.page_profile('<form><input type="password" name="p"></form>', 200, "https://example.com")
    assert login_page["public_content"] is False and login_page["login_found"] is True
