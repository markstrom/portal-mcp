"""HTTP transport and the real `local` login provider (the latter needs Chrome or Playwright Chromium)."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from portal_mcp.config import Paths
from portal_mcp.login.base import LoginError, LoginManager
from portal_mcp.login.local import LocalLoginProvider
from portal_mcp.registry import Registry
from portal_mcp.server import build_server, run_http
from tests.conftest import mock_manifest_yaml, write_manifest


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_streamable_http_lists_tools(paths, store, downloads, mock_base_url):
    write_manifest(paths.services, "mockportal", mock_manifest_yaml(mock_base_url))
    registry = Registry(paths, store, LoginManager(_NoLogin(), store), downloads)
    server, state = build_server(registry)
    port = _free_port()
    task = asyncio.create_task(run_http(server, registry, state, "127.0.0.1", port))
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        for _ in range(100):  # wait for uvicorn to listen
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)
        async with streamable_http_client(url) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    names = {t.name for t in listed.tools}
    assert "mockportal_list_invoices" in names


def test_http_refuses_non_local_bind(paths, store, downloads):
    registry = Registry(paths, store, LoginManager(_NoLogin(), store), downloads)
    server, state = build_server(registry)
    with pytest.raises(SystemExit):
        asyncio.run(run_http(server, registry, state, "0.0.0.0", 8765))


class _NoLogin:
    name = "none"

    async def login(self, manifest, profile):  # pragma: no cover - must not be called
        raise AssertionError("login should not run in this test")


def _browser_available() -> bool:
    """Google Chrome (preferred by the provider) or Playwright's Chromium must be on this machine."""
    if Path("/Applications/Google Chrome.app").exists():
        return True
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


@pytest.mark.skipif(not _browser_available(), reason="no Google Chrome or Playwright Chromium installed")
async def test_local_provider_times_out_and_cleans_up(paths: Paths, mock_base_url, monkeypatch):
    import yaml

    from portal_mcp.manifest import Manifest

    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest = Manifest.model_validate(yaml.safe_load(mock_manifest_yaml(mock_base_url)))
    provider = LocalLoginProvider(paths, timeout_seconds=3, poll_seconds=0.2)
    with pytest.raises(LoginError, match="timed out"):
        await provider.login(manifest, "default")
    assert (paths.profiles / "mockportal" / "default").is_dir()


@pytest.mark.skipif(not _browser_available(), reason="no Google Chrome or Playwright Chromium installed")
async def test_real_browser_command_line_has_no_no_sandbox_flag(tmp_path):
    """Reads the command line the browser really got, from chrome://version."""
    from playwright.async_api import async_playwright

    from portal_mcp.login.local import _launch

    async with async_playwright() as playwright:
        context = await _launch(playwright, tmp_path / "profile", True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("chrome://version")
            command_line = await page.inner_text("#command_line")
        finally:
            await context.close()
    assert command_line.strip()
    assert "--no-sandbox" not in command_line


@pytest.mark.skipif(not _browser_available(), reason="no Google Chrome or Playwright Chromium installed")
async def test_polling_a_redirecting_check_adds_no_cookies_to_the_profile(tmp_path, mock_base_url):
    """Regression: each poll of a redirecting session check used to add a nonce cookie to the profile."""
    import yaml
    from playwright.async_api import async_playwright

    from portal_mcp.login.local import _capture, _launch
    from portal_mcp.manifest import Manifest

    manifest = Manifest.model_validate(yaml.safe_load(
        f"name: demo\nbase_url: {mock_base_url}\nlogin_url: {mock_base_url}/login\nsession: {{check: /account}}\n"
    ))
    async with async_playwright() as playwright:
        context = await _launch(playwright, tmp_path / "profile", True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(manifest.login_url)
            for _ in range(10):
                assert await _capture(context, page, manifest) is None  # not logged in yet
            names = [c["name"] for c in await context.cookies([mock_base_url])]
        finally:
            await context.close()
    assert not [n for n in names if n.startswith("nonce.")]
