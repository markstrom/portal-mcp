"""The recorder against the mock service, in a real (headless) browser.

Login is seeded once by filling in the mock's login form, so the recorder finds a valid
session in the persistent profile. Needs Google Chrome or Playwright's Chromium.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from tests.conftest import mock_manifest_yaml, write_manifest
from tests.e2e.test_http_and_login import _browser_available
from tests.mock_service.data import PASSWORD, USERNAME

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

pytestmark = pytest.mark.skipif(not _browser_available(), reason="no Google Chrome or Playwright Chromium installed")


async def _seed_login(profile_dir: Path, base_url: str) -> None:
    from playwright.async_api import async_playwright

    from portal_mcp.login.local import _launch

    profile_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        context = await _launch(p, profile_dir, True)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(base_url + "/login")
        await page.fill("input[name=username]", USERNAME)
        await page.fill("input[name=password]", PASSWORD)
        await page.click("button")
        await page.wait_for_url(base_url + "/")
        await context.close()


async def test_recorder_blocks_and_captures_writes(tmp_path, mock_base_url, monkeypatch):
    import analyze
    import record

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)

    from portal_mcp.manifest import load_manifest_file

    out = tmp_path / "rec"
    summary = await record.record(
        load_manifest_file(manifest_path),
        ["/api/invoices", "/reports/new", "/payments/new", "/api/invoices/inv-001/delete"],
        out,
        timeout=30,
        profile="default",
    )

    assert summary["logged_in"] is True
    assert summary["captured_writes"] == 2
    reasons = {b["path"]: b["reason"] for b in summary["blocked"]}
    assert "side effect" in reasons["/api/invoices/inv-001/delete"]
    assert "POST is blocked" in reasons["/api/payments"]

    captured = json.loads((out / "captured_writes.json").read_text())
    by_path = {c["path"]: c for c in captured}
    assert json.loads(by_path["/api/reports"]["body"]) == {"title": "Form title", "text": "Form text"}
    assert "amount=125" in by_path["/api/payments"]["body"]

    # Nothing reached the service: no payment was created and the invoice still exists.
    async with httpx.AsyncClient(base_url=mock_base_url) as client:
        login = await client.post("/login", data={"username": USERNAME, "password": PASSWORD})
        assert login.status_code == 303
        payments = (await client.get("/api/payments")).json()["items"]
        invoices = (await client.get("/api/invoices", params={"page_size": 50})).json()["items"]
    assert payments == []
    assert any(i["id"] == "inv-001" for i in invoices)

    writes = analyze.analyze_writes(captured, "127.0.0.1")
    kinds = {w["path"]: w for w in writes}
    assert kinds["/api/payments"]["suggested_kind"] == "binding"
    assert kinds["/api/payments"]["encoding"] == "form"
    assert kinds["/api/payments"]["values"] == {}  # values hidden by default
    assert kinds["/api/reports"]["encoding"] == "json"
    assert "Form title" not in json.dumps(writes)
