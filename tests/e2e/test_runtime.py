"""End-to-end tests: real MCP protocol (in memory) against the mock portal over HTTP.

Login is replaced by a fake provider that performs the mock service's form login with
test credentials. The real LocalLoginProvider (visible browser) is covered by a separate
test that only runs when a Playwright Chromium is installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import portal_mcp.service as service_module
from portal_mcp.login.base import LoginManager
from portal_mcp.registry import Registry
from portal_mcp.server import build_server
from portal_mcp.service import ServiceClient
from portal_mcp.session_store import StoredSession
from tests.conftest import mock_manifest_yaml, write_manifest
from tests.mock_service.data import PASSWORD, USERNAME


class FakeLogin:
    """Performs the mock service's form login. Optionally waits on a gate first."""

    name = "fake"

    def __init__(self):
        self.calls = 0
        self.gate: asyncio.Event | None = None

    async def login(self, manifest, profile):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        async with httpx.AsyncClient(base_url=manifest.base_url) as client:
            response = await client.post(
                "/login", data={"username": USERNAME, "password": PASSWORD}, follow_redirects=False
            )
        assert response.status_code == 303, response.text
        cookies = [{"name": n, "value": v, "domain": "127.0.0.1", "path": "/"} for n, v in response.cookies.items()]
        return StoredSession(cookies=cookies, csrf=response.cookies.get("csrf"))


@pytest.fixture
def setup(paths, store, downloads, mock_base_url):
    provider = FakeLogin()
    write_manifest(paths.services, "mockportal", mock_manifest_yaml(mock_base_url))
    logins = LoginManager(provider, store)
    registry = Registry(paths, store, logins, downloads)
    server, _state = build_server(registry)
    return {"registry": registry, "server": server, "provider": provider, "store": store, "base": mock_base_url, "paths": paths, "downloads": downloads}


def _text(result) -> str:
    return result.content[0].text


async def test_exposes_read_and_file_tools_only(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        listed = await client.list_tools()
    names = {t.name for t in listed.tools}
    assert "mockportal_list_invoices" in names
    assert "mockportal_download_invoice_pdf" in names
    assert "mockportal_create_report" not in names  # write tools come in phase 3
    assert {"list_services", "forget_session", "logout"} <= names


async def test_list_invoices_logs_in_and_applies_pick_and_redact(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        result = await client.call_tool("mockportal_list_invoices", {"page": 1, "page_size": 2})
    assert not result.isError, _text(result)
    data = json.loads(_text(result))
    assert setup["provider"].calls == 1
    assert [item["id"] for item in data["items"]] == ["inv-001", "inv-002"]
    owner = data["items"][0]["owner"]
    assert owner == {"name": "Testkund Ett"}  # national_id and address redacted
    assert set(data["items"][0]) == {"id", "date", "amount", "status", "owner"}  # pick applied
    assert "total_pages" not in data  # fields outside the pick list are dropped


async def test_pagination_arguments_reach_the_service(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        result = await client.call_tool("mockportal_list_invoices", {"page": 2, "page_size": 2})
    data = json.loads(_text(result))
    assert [item["id"] for item in data["items"]] == ["inv-003", "inv-004"]


async def test_file_tool_saves_the_pdf(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        result = await client.call_tool("mockportal_download_invoice_pdf", {"invoice_id": "inv-002"})
    assert not result.isError, _text(result)
    info = json.loads(_text(result))
    saved = Path(info["saved_to"])
    assert saved.parent == setup["downloads"]
    assert saved.name == "invoice-inv-002.pdf"
    assert saved.read_bytes().startswith(b"%PDF")


async def test_session_expiry_triggers_one_relogin(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        first = await client.call_tool("mockportal_list_invoices", {})
        assert not first.isError, _text(first)

        # The service ends the session on its side (e.g. timeout). The local copy still looks valid.
        session = setup["store"].load("mockportal", "default")
        csrf = session.csrf
        async with httpx.AsyncClient(base_url=setup["base"], cookies=_jar(session)) as raw:
            ended = await raw.post("/api/logout", headers={"x-csrf-token": csrf})
        assert ended.status_code == 204

        second = await client.call_tool("mockportal_list_invoices", {})
    assert not second.isError, _text(second)
    assert setup["provider"].calls == 2


async def test_cached_read_skips_the_service(paths, store, downloads, mock_base_url):
    provider = FakeLogin()
    write_manifest(paths.services, "mockportal", mock_manifest_yaml(mock_base_url, cache=60))
    registry = Registry(paths, store, LoginManager(provider, store), downloads)
    server, _ = build_server(registry)
    async with create_connected_server_and_client_session(server) as client:
        await client.call_tool("mockportal_list_invoices", {"page": 1})
        store.delete("mockportal", "default")  # without a login this call would fail
        again = await client.call_tool("mockportal_list_invoices", {"page": 1})
    assert not again.isError, _text(again)
    assert provider.calls == 1


async def test_rate_limit_is_enforced(paths, store, downloads, mock_base_url):
    provider = FakeLogin()
    write_manifest(paths.services, "mockportal", mock_manifest_yaml(mock_base_url, rate=2))
    registry = Registry(paths, store, LoginManager(provider, store), downloads)
    server, _ = build_server(registry)
    async with create_connected_server_and_client_session(server) as client:
        assert not (await client.call_tool("mockportal_list_invoices", {})).isError
        assert not (await client.call_tool("mockportal_list_invoices", {})).isError
        third = await client.call_tool("mockportal_list_invoices", {})
    assert third.isError
    assert "rate limit" in _text(third)


async def test_logout_tool_calls_service_and_deletes_local_session(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        await client.call_tool("mockportal_list_invoices", {})
        result = await client.call_tool("logout", {"service": "mockportal"})
        assert not result.isError, _text(result)
        report = json.loads(_text(result))
        assert report["remote"] == "logout endpoint answered HTTP 204"
        assert setup["store"].load("mockportal", "default") is None

        status = json.loads(_text(await client.call_tool("list_services", {})))
    assert status["services"]["mockportal"]["profiles"] == {"default": None, "second": None}


async def test_forget_session_only_deletes_locally(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        await client.call_tool("mockportal_list_invoices", {})
        result = await client.call_tool("forget_session", {"service": "mockportal", "profile": "default"})
    assert json.loads(_text(result))["removed_profiles"] == ["default"]
    assert setup["store"].load("mockportal", "default") is None


async def test_broken_service_is_isolated(setup):
    write_manifest(setup["paths"].services, "broken", "name: broken\nsession: nope\n")
    setup["registry"].reload()
    assert "broken" in setup["registry"].errors
    async with create_connected_server_and_client_session(setup["server"]) as client:
        result = await client.call_tool("mockportal_list_invoices", {})
    assert not result.isError, _text(result)


async def test_unknown_profile_is_rejected(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        result = await client.call_tool("mockportal_list_invoices", {"profile": "nope"})
    assert result.isError
    assert "unknown profile" in _text(result)


async def test_manifest_edit_is_picked_up_without_restart(setup):
    assert setup["registry"].reload() is False  # nothing changed yet
    path = setup["paths"].services / "mockportal.yaml"
    extra = (
        "  - name: list_clients\n    kind: read\n    method: GET\n    path: /api/invoices\n"
    )
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    assert setup["registry"].reload() is True
    async with create_connected_server_and_client_session(setup["server"]) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "mockportal_list_clients" in names


async def test_login_in_progress_does_not_block_and_completes_in_background(setup):
    provider = setup["provider"]
    provider.gate = asyncio.Event()
    client_obj = setup["registry"]._clients["mockportal"]
    client_obj.login_wait_seconds = 0.2

    async with create_connected_server_and_client_session(setup["server"]) as client:
        first = await client.call_tool("mockportal_list_invoices", {})
        assert first.isError
        assert "login window for mockportal is open" in _text(first)

        status = json.loads(_text(await client.call_tool("list_services", {})))
        assert status["login_in_progress"] == {"service": "mockportal", "profile": "default"}

        provider.gate.set()  # the user finishes logging in
        for _ in range(100):
            if setup["store"].load("mockportal", "default") is not None:
                break
            await asyncio.sleep(0.02)

        second = await client.call_tool("mockportal_list_invoices", {})
    assert not second.isError, _text(second)
    assert provider.calls == 1


async def test_login_refused_without_a_display(setup, monkeypatch):
    monkeypatch.setattr(service_module, "has_display", lambda: False)
    async with create_connected_server_and_client_session(setup["server"]) as client:
        result = await client.call_tool("mockportal_list_invoices", {})
    assert result.isError
    assert "graphical desktop" in _text(result)
    assert setup["provider"].calls == 0


def _jar(session: StoredSession) -> httpx.Cookies:
    jar = httpx.Cookies()
    for cookie in session.cookies:
        jar.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    return jar
