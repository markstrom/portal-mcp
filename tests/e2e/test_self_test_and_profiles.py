"""self_test against saved structures, and several profiles per service."""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from portal_mcp.login.base import LoginManager
from portal_mcp.registry import Registry
from portal_mcp.server import build_server
from tests.conftest import mock_manifest_yaml, write_manifest
from tests.e2e.test_runtime import FakeLogin

EXTRA_TOOLS = """  - name: invoices_page
    kind: read
    method: GET
    path: /api/invoices
    params:
      type: object
      properties:
        page: {type: integer}
      required: [page]
    test_args: {page: 2}
  - name: invoices_page_untested
    kind: read
    method: GET
    path: /api/invoices
    params:
      type: object
      properties:
        page: {type: integer}
      required: [page]
"""


class ProfileLogin(FakeLogin):
    def __init__(self):
        super().__init__()
        self.profiles: list[str] = []

    async def login(self, manifest, profile):
        self.profiles.append(profile)
        return await super().login(manifest, profile)


@pytest.fixture
def setup(paths, store, downloads, mock_base_url):
    provider = ProfileLogin()
    write_manifest(paths.services, "mockportal", mock_manifest_yaml(mock_base_url, extra_tools=EXTRA_TOOLS))
    registry = Registry(paths, store, LoginManager(provider, store), downloads)
    server, _ = build_server(registry)
    return {"server": server, "registry": registry, "provider": provider, "store": store, "paths": paths}


def _json(result):
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


async def test_self_test_saves_baseline_then_compares(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        first = _json(await client.call_tool("self_test", {"service": "mockportal"}))
        statuses = {r["tool"]: r for r in first["results"]}
        assert statuses["list_invoices"]["status"] == "baseline_saved"
        assert statuses["invoices_page"]["status"] == "baseline_saved"
        assert statuses["invoices_page_untested"]["status"] == "skipped"
        assert "needs test_args" in statuses["invoices_page_untested"]["reason"]
        assert statuses["download_invoice_pdf"]["status"] == "skipped"
        assert statuses["pay"]["status"] == "skipped"
        assert statuses["send_message"]["reason"] == "not exposed"

        baseline_file = setup["paths"].services / "mockportal.schemas.json"
        text = baseline_file.read_text()
        assert "Testkund" not in text and "inv-001" not in text  # structure only, no values

        second = _json(await client.call_tool("self_test", {"service": "mockportal"}))
        assert {r["tool"]: r["status"] for r in second["results"]}["list_invoices"] == "ok"
        assert "match" in second["summary"]

        # The saved structure had a field that the service no longer returns.
        data = json.loads(text)
        data["tools"]["list_invoices"]["schema"]["items"][0]["legacy_field"] = "string"
        baseline_file.write_text(json.dumps(data))
        third = _json(await client.call_tool("self_test", {"service": "mockportal"}))
        changed = {r["tool"]: r for r in third["results"]}["list_invoices"]
        assert changed["status"] == "changed"
        assert changed["differences"]["removed"] == ["items[].legacy_field"]
        assert "/portal-mcp:update mockportal" in third["summary"]

        fourth = _json(await client.call_tool("self_test", {"service": "mockportal", "update_baseline": True}))
        assert {r["tool"]: r["status"] for r in fourth["results"]}["list_invoices"] == "baseline_updated"
        fifth = _json(await client.call_tool("self_test", {"service": "mockportal"}))
        assert {r["tool"]: r["status"] for r in fifth["results"]}["list_invoices"] == "ok"


async def test_profiles_have_separate_sessions_and_logout(setup):
    store = setup["store"]
    async with create_connected_server_and_client_session(setup["server"]) as client:
        _json(await client.call_tool("mockportal_list_invoices", {}))
        _json(await client.call_tool("mockportal_list_invoices", {"profile": "second"}))
        assert setup["provider"].profiles == ["default", "second"]
        assert store.load("mockportal", "default").cookies != store.load("mockportal", "second").cookies

        out = _json(await client.call_tool("logout", {"service": "mockportal", "profile": "second"}))
        assert out["profiles"] == ["second"]
        assert store.load("mockportal", "second") is None
        assert store.load("mockportal", "default") is not None  # the other profile is untouched

        status = _json(await client.call_tool("list_services", {}))
        profiles = status["services"]["mockportal"]["profiles"]
        assert profiles["default"] is not None and profiles["second"] is None

        everything = _json(await client.call_tool("logout", {"service": "mockportal"}))
        assert everything["profiles"] == ["default", "second"]
        assert store.load("mockportal", "default") is None


async def test_self_test_uses_the_given_profile(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        out = _json(await client.call_tool("self_test", {"service": "mockportal", "profile": "second"}))
    assert out["profile"] == "second"
    assert setup["provider"].profiles == ["second"]
