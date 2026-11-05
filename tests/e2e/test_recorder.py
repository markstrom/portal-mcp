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
        reuse_profile=True,  # the test logged in to the persistent profile above
    )

    assert summary["login_verified"] is True
    assert summary["profile"] == {"clean": False, "reused": True}
    assert summary["captured_writes"] == 2
    reasons = {b["path"]: b["reason"] for b in summary["blocked_other"]}
    # a side-effect path is refused before the browser even asks for it
    assert {v["path"]: v["status"] for v in summary["visited"]}["/api/invoices/inv-001/delete"] == "refused"
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


async def test_recorder_uses_a_clean_profile_by_default(tmp_path, mock_base_url, monkeypatch):
    import record

    from portal_mcp.manifest import load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    persistent = home / "profiles" / "mockportal" / "default"
    await _seed_login(persistent, mock_base_url)  # a logged-in persistent profile exists

    out = tmp_path / "rec"
    summary = await record.record(load_manifest_file(manifest_path), [], out, timeout=3, profile="default",
                                  poll_seconds=0.5)
    # The clean profile does not have the persistent login, so the login is not verified...
    assert summary["profile"] == {"clean": True, "reused": False}
    assert summary["login_verified"] is False
    assert "not verified" in summary["error"]
    # ...the temporary profile is gone, and the persistent one was not touched.
    assert not (out / "browser-profile").exists()
    assert persistent.is_dir()
    # The analyzer reads this marker and refuses the recording.
    assert json.loads((out / "recording.json").read_text())["login_verified"] is False


async def test_discover_lists_menu_links_after_login_and_waits_for_the_choice(tmp_path, mock_base_url, monkeypatch):
    import asyncio

    import record

    from portal_mcp.manifest import load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)
    out = tmp_path / "rec"

    async def choose():
        links_file = out / "menu_links.json"
        for _ in range(300):
            if links_file.exists():
                break
            await asyncio.sleep(0.1)
        found = json.loads(links_file.read_text())
        paths = [link["path"] for link in found["links"]]
        assert "/invoices-page" in paths and "/profile?tab=1" in paths
        assert not any("logout" in p or "delete" in p or p.endswith(".pdf") for p in paths)
        assert found["dropped"]["other host"] == 1
        (out / "visit.json").write_text(json.dumps(["/invoices-page", "/api/logout"]))

    chooser = asyncio.create_task(choose())
    summary = await record.record(
        load_manifest_file(manifest_path), [], out, timeout=30, profile="default",
        reuse_profile=True, discover=True, choice_timeout=30,
    )
    await chooser
    assert summary["login_verified"] is True
    assert summary["menu_links"]["count"] >= 3
    visited = {v["path"]: v["status"] for v in summary["visited"]}
    assert "/invoices-page" in visited
    assert visited["/api/logout"] == "refused"  # a side-effect choice is never visited


async def test_check_candidates_are_tried_logged_out_and_logged_in(tmp_path, mock_base_url, monkeypatch):
    import record

    from portal_mcp.manifest import load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)

    summary = await record.record(
        load_manifest_file(manifest_path), [], tmp_path / "rec", timeout=30, profile="default",
        reuse_profile=True,
        check_candidates=["/api/invoices", "/account", "/api/logout", "https://other.example.org/x"],
    )
    assert summary["login_verified"] is True
    assert summary["session_check"]["logged_in_answer"] == 200
    report = {c["path"]: c for c in summary["check_candidates"]}

    invoices = report["/api/invoices"]
    assert invoices["usable"] is True
    assert invoices["logged_out"]["status"] == 401 and invoices["logged_in"]["status"] == 200
    assert invoices["logged_in"]["content_type"] == "application/json"

    account = report["/account"]
    assert account["usable"] is False and "redirects" in account["reason"]
    assert account["logged_out"]["status"] == 302 and account["logged_in"]["status"] == 200

    assert report["/api/logout"]["usable"] is False and "side effect" in report["/api/logout"]["reason"]
    assert report["https://other.example.org/x"]["usable"] is False


async def test_recording_suggests_a_session_check_when_the_current_one_redirects(tmp_path, mock_base_url, monkeypatch):
    import record

    from portal_mcp.manifest import load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    text = mock_manifest_yaml(mock_base_url).replace("check: /api/session", "check: /account")
    manifest_path = write_manifest(home / "services", "mockportal", text)
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)

    summary = await record.record(
        load_manifest_file(manifest_path), ["/"], tmp_path / "rec", timeout=30, profile="default",
        reuse_profile=True,
    )
    assert summary["session_check"]["verdict"] == "redirects"
    suggestions = summary["suggested_session_checks"]
    by_path = {s["path"]: s for s in suggestions}
    assert suggestions[0]["path"] == "/api/invoices" and suggestions[0]["usable"] is True
    assert by_path["/api/invoices"]["logged_out"]["status"] == 401
    assert by_path["/api/invoices"]["logged_in"]["status"] == 200
    assert by_path["/api/public-info"]["usable"] is False
    assert "200 even when logged out" in by_path["/api/public-info"]["reason"]


async def test_no_suggestions_when_the_current_check_already_answers_401(tmp_path, mock_base_url, monkeypatch):
    import record

    from portal_mcp.manifest import load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)
    summary = await record.record(
        load_manifest_file(manifest_path), ["/"], tmp_path / "rec", timeout=30, profile="default",
        reuse_profile=True,
    )
    assert summary["session_check"]["verdict"] == "rejects"
    assert summary["suggested_session_checks"] is None


async def test_explore_clicks_safe_controls_and_never_actions(tmp_path, mock_base_url, monkeypatch):
    import record

    from portal_mcp.manifest import load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)
    out = tmp_path / "rec"

    summary = await record.record(
        load_manifest_file(manifest_path), ["/cases"], out, timeout=30, profile="default",
        reuse_profile=True, explore=True, click_wait=2,
    )
    assert summary["login_verified"] is True
    page = summary["explored"]["pages"][0]
    controls = {c["text"]: c for c in json.loads((out / "explored.json").read_text())[0]["controls"]}
    # The tab loads JSON only when clicked; the hidden "Show more" is found after "More" opens it.
    assert controls["Open cases"]["json_reads"] >= 1
    assert "Show more" in controls and controls["Show more"]["json_reads"] >= 1
    # Actions, a form's submit button and an unlabeled button are never clicked.
    assert not {"Delete all", "Log out", "Next", ""} & set(controls)
    assert sum(1 for c in controls if c == "(icon that expands)") == 1  # the plain empty button is skipped
    assert page["skipped"]["looks like an action"] >= 2
    assert page["skipped"]["submits a form"] == 1
    # A write caused by a click is captured, not sent.
    assert controls["Mark as read"]["captured_writes"] == 1
    assert any(c["path"] == "/api/messages" for c in json.loads((out / "captured_writes.json").read_text()))
    # A click that leaves the page: the recorder came back and went on.
    assert controls["Go to invoices"]["left_page"] is True
    # The cookie banner was declined first, and its accept button never clicked.
    assert page["consent_banner"] == "declined"
    assert "Acceptera alla" not in controls
    # The search field was filled and submitted, and its JSON read recorded.
    searches = json.loads((out / "explored.json").read_text())[0]["searches"]
    assert page["searched"] == 1 and searches[0]["json_reads"] >= 1
    # An icon toggle without a label is clicked because it expands.
    assert controls["(icon that expands)"]["json_reads"] >= 1

    # Nothing reached the service: the invoice the delete button points at still exists.
    async with httpx.AsyncClient(base_url=mock_base_url) as client:
        await client.post("/login", data={"username": USERNAME, "password": PASSWORD})
        invoices = (await client.get("/api/invoices", params={"page_size": 50})).json()["items"]
    assert any(i["id"] == "inv-002" for i in invoices)


async def test_without_explore_nothing_is_clicked(tmp_path, mock_base_url, monkeypatch):
    import record

    from portal_mcp.manifest import load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)
    out = tmp_path / "rec"
    summary = await record.record(load_manifest_file(manifest_path), ["/cases"], out, timeout=30,
                                  profile="default", reuse_profile=True)
    assert summary["explored"] is None
    assert not (out / "explored.json").exists()
    assert summary["captured_writes"] == 0


async def test_a_public_site_is_recorded_without_a_login(tmp_path, mock_base_url, monkeypatch):
    import record

    from portal_mcp.manifest import Manifest

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest = Manifest.model_validate({
        "name": "opensite", "base_url": mock_base_url, "session": {"type": "none"}, "tools": [],
    })
    out = tmp_path / "rec"
    summary = await record.record(manifest, ["/cases"], out, timeout=30, profile="default",
                                  explore=True, click_wait=2)
    assert summary["error"] is None
    assert summary["public"] is True and summary["session_check"] is None
    # No login: the page itself has no session, so its writes are still captured, not sent.
    assert summary["visited"] == [{"path": "/cases", "status": "ok", "depth": 1}]
    assert summary["captured_writes"] >= 1
    marker = json.loads((out / "recording.json").read_text())
    assert marker["public"] is True


async def test_the_apps_token_is_read_after_login_and_reported_by_name(tmp_path, mock_base_url, monkeypatch):
    from playwright.async_api import async_playwright

    import record

    from portal_mcp.login.local import _launch, read_data_token
    from portal_mcp.manifest import Manifest, load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest = Manifest.model_validate({
        "name": "withapp", "base_url": mock_base_url, "login_url": mock_base_url + "/login",
        "session": {"type": "cookies", "check": "/api/session"}, "data_hosts": ["api.example.net"],
        "data_auth": {"source": "sessionStorage:oidc.user:*", "field": "access_token", "page": "/app"},
    })
    profile = tmp_path / "profile"
    profile.mkdir()
    async with async_playwright() as p:
        context = await _launch(p, profile, True)
        page = context.pages[0] if context.pages else await context.new_page()
        assert await read_data_token(page, manifest, timeout=10, poll=0.2) == "app-token-1"
        await context.close()

    # The recorder reports where the token is kept, by key name and JSON fields, never the value.
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)
    out = tmp_path / "rec"
    summary = await record.record(load_manifest_file(manifest_path), ["/app"], out, timeout=30,
                                  profile="default", reuse_profile=True)
    entry = next(t for t in summary["token_storage"] if t["source"].startswith("sessionStorage:oidc.user:"))
    assert "access_token" in entry["json_fields"]
    assert "app-token-1" not in json.dumps(summary)


async def test_links_on_visited_pages_are_followed_up_to_the_cap(tmp_path, mock_base_url, monkeypatch):
    import record

    from portal_mcp.manifest import load_manifest_file

    home = tmp_path / "home"
    monkeypatch.setenv("PORTAL_MCP_HOME", str(home))
    monkeypatch.setenv("PORTAL_MCP_HEADLESS", "1")
    manifest_path = write_manifest(home / "services", "mockportal", mock_manifest_yaml(mock_base_url))
    await _seed_login(home / "profiles" / "mockportal" / "default", mock_base_url)

    summary = await record.record(load_manifest_file(manifest_path), ["/"], tmp_path / "deep", timeout=30,
                                  profile="default", reuse_profile=True, depth=2)
    visited = {v["path"]: v for v in summary["visited"]}
    assert visited["/"]["depth"] == 1
    assert visited["/invoices-page"]["depth"] == 2 and visited["/profile?tab=1"]["depth"] == 2
    assert not any("logout" in p or "delete" in p or p.endswith(".pdf") for p in visited)
    assert summary["links_followed"] >= 3

    capped = await record.record(load_manifest_file(manifest_path), ["/"], tmp_path / "capped", timeout=30,
                                 profile="default", reuse_profile=True, depth=2, max_pages=2)
    statuses = [v["status"] for v in capped["visited"]]
    assert statuses.count("ok") + sum(s.startswith("error") for s in statuses) == 2
    assert "skipped: max_pages" in statuses
