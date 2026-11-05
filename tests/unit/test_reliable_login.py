"""Login verification, diagnostics, menu links, clean profiles and HAR cleanup (issue: reliable login)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
import pytest
import yaml

from portal_mcp.config import Paths
from portal_mcp.diagnostics import cookie_summary, header_too_large_message, is_header_too_large, is_tracking_host
from portal_mcp.login.base import LoginError
from portal_mcp.login.local import (
    LocalLoginProvider,
    LoginObservation,
    check_profile_lock,
    login_not_verified_message,
    preflight_check,
    wait_for_login,
)
from portal_mcp.manifest import Manifest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import links  # noqa: E402
import record  # noqa: E402

HOST = "app.example.com"


def _manifest(check: str = "/api/session") -> Manifest:
    return Manifest.model_validate(yaml.safe_load(f"""
name: example
base_url: https://{HOST}
login_url: https://login.example.com/start
login_hosts: [login.example.com]
session: {{check: {check}}}
"""))


def _transport(status: int, *, body: str = "", location: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"location": location} if location else {}
        return httpx.Response(status, headers=headers, text=body)

    return httpx.MockTransport(handler)


# ----- B: login verification ------------------------------------------------

@pytest.mark.parametrize(
    "status,location,verdict",
    [(401, None, "rejects"), (403, None, "rejects"), (302, "https://login.example.com/authorize", "redirects"),
     (200, None, "open"), (500, None, "error")],
)
async def test_preflight_verdicts(status, location, verdict):
    result = await preflight_check(_manifest(), transport=_transport(status, location=location))
    assert result.verdict == verdict


async def test_redirecting_check_is_a_clear_warning_and_names_the_login_host():
    result = await preflight_check(_manifest("/home/"), transport=_transport(302, location="https://login.example.com/x"))
    text = result.explain(_manifest("/home/"))
    assert "redirects to login.example.com" in text and "a redirect never counts" in text


async def test_check_that_answers_200_without_login_stops_the_login(tmp_path, monkeypatch):
    async def open_check(manifest, transport=None):
        from portal_mcp.login.local import CheckResult
        return CheckResult(status=200)

    monkeypatch.setattr("portal_mcp.login.local.preflight_check", open_check)
    provider = LocalLoginProvider(Paths(base=tmp_path))
    with pytest.raises(LoginError, match="cannot tell a logged-out user from a logged-in one"):
        await provider.login(_manifest(), "default")
    assert not (tmp_path / "profiles").exists()  # no browser was started


class _Context:
    async def cookies(self, urls):
        return [{"name": "sid", "value": "SECRET-VALUE", "domain": HOST, "path": "/"}]


async def test_redirecting_check_is_never_logged_in_and_the_timeout_says_why():
    transport = _transport(302, location="https://login.example.com/authorize")
    with pytest.raises(LoginError) as info:
        await wait_for_login(_Context(), None, _manifest("/home/"), timeout=0.3, poll=0.1, transport=transport)
    message = str(info.value)
    assert "not verified" in message and "302 x" in message and "redirecting to login.example.com" in message


# ----- D: oversized headers -------------------------------------------------

@pytest.mark.parametrize(
    "status,body,expected",
    [
        (431, "", True),
        (400, "Bad Request - Request Too Long. The size of the request headers is too long.", True),
        (400, "400 Bad Request: Request Header Or Cookie Too Large", True),
        (400, "Invalid field 'amount'", False),
        (500, "request headers too long", False),
    ],
)
def test_header_too_large_detection(status, body, expected):
    assert is_header_too_large(status, body) is expected


def test_header_message_shows_names_and_counts_but_never_values():
    cookies = [{"name": f"OpenIdConnect.nonce.{'A' * 30}{i}", "value": f"VALUE-{i}", "domain": HOST, "path": "/"}
               for i in range(5)]
    cookies.append({"name": "session", "value": "TOPSECRET", "domain": HOST, "path": "/"})
    summary = cookie_summary(cookies)
    assert summary["count"] == 6
    assert summary["by_name_pattern"]["OpenIdConnect.nonce.<id>"] == 5
    message = header_too_large_message("example", HOST, cookies)
    dumped = json.dumps(summary) + message
    assert "VALUE-" not in dumped and "TOPSECRET" not in dumped
    assert "clean profile" in message and HOST in message


async def test_check_that_answers_header_too_large_stops_the_login_with_the_cookie_summary():
    transport = _transport(400, body="The size of the request headers is too long")
    with pytest.raises(LoginError) as info:
        await wait_for_login(_Context(), None, _manifest(), timeout=5, poll=0.1, transport=transport)
    assert "headers were too large" in str(info.value) and "SECRET-VALUE" not in str(info.value)


# ----- C: menu links --------------------------------------------------------

RAW = [
    {"href": f"https://{HOST}/invoices", "text": "Invoices", "in_menu": True},
    {"href": f"https://{HOST}/invoices#top", "text": "Invoices", "in_menu": False},
    {"href": f"https://{HOST}/logout", "text": "Log out", "in_menu": True},
    {"href": f"https://{HOST}/account/settings", "text": "Logga ut", "in_menu": True},
    {"href": f"https://{HOST}/terms.pdf", "text": "Terms", "in_menu": False},
    {"href": "https://login.example.com/profile", "text": "Account", "in_menu": True},
    {"href": "mailto:help@example.com", "text": "Mail", "in_menu": False},
    {"href": f"https://{HOST}/reports?year=2026", "text": "Reports", "in_menu": False},
]


def test_menu_links_only_after_verified_login():
    with pytest.raises(links.NotLoggedIn):
        links.filter_menu_links(RAW, HOST, logged_in=False)


def test_menu_links_are_same_host_reading_links_only():
    found = links.filter_menu_links(RAW, HOST, logged_in=True)
    assert [link["path"] for link in found["links"]] == ["/invoices", "/reports?year=2026"]
    assert found["dropped"] == {"looks like an action": 2, "file download": 1, "other host": 1, "not a web link": 1}


@pytest.mark.parametrize("path,ok", [("/invoices", True), ("//evil.example/x", False), ("https://x/y", False),
                                     ("/api/logout", False), (42, False)])
def test_only_safe_chosen_paths_are_visited(path, ok):
    assert (links.valid_visit(path, HOST) is not None) is ok


# ----- A: clean profile by default ------------------------------------------

def test_recording_uses_a_clean_temporary_profile_by_default(tmp_path):
    paths = Paths(base=tmp_path / "home")
    out = tmp_path / "rec"
    directory, temporary = record.profile_for_run(paths, _manifest(), "default", out, reuse_profile=False)
    assert temporary is True and directory == out / "browser-profile"
    directory, temporary = record.profile_for_run(paths, _manifest(), "default", out, reuse_profile=True)
    assert temporary is False and directory == paths.profiles / "example" / "default"


# ----- E: browser start -----------------------------------------------------

def test_stale_profile_lock_is_removed(tmp_path):
    import socket

    (tmp_path / "SingletonLock").symlink_to(f"{socket.gethostname()}-999999")
    (tmp_path / "SingletonSocket").symlink_to("/nonexistent/socket")
    messages: list[str] = []
    check_profile_lock(tmp_path, messages.append)
    assert not (tmp_path / "SingletonLock").is_symlink()
    assert not (tmp_path / "SingletonSocket").is_symlink()
    assert "stale browser lock" in messages[0]


def test_profile_lock_held_by_a_running_browser_is_a_clear_error(tmp_path):
    import os
    import socket

    (tmp_path / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    with pytest.raises(LoginError, match="in use by another browser process"):
        check_profile_lock(tmp_path)


async def test_browser_start_timeout_is_an_error_with_a_cause(tmp_path):
    import asyncio

    from portal_mcp.login.local import _launch

    class Hanging:
        async def launch_persistent_context(self, *args, **kwargs):
            await asyncio.sleep(10)

    class Playwright:
        chromium = Hanging()

    with pytest.raises(LoginError, match="did not start within 0 seconds"):
        await _launch(Playwright(), tmp_path, True, start_timeout=0.2)


# ----- sandbox on every start path ------------------------------------------

def test_no_code_starts_a_browser_outside_the_sandboxed_launcher():
    pattern = re.compile(r"launch_persistent_context\(|chromium\.launch\(|firefox\.launch\(|webkit\.launch\(")
    offenders = []
    for path in list((ROOT / "runtime").rglob("*.py")) + list((ROOT / "skills").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    # exactly one place starts a browser: _launch, which always turns the sandbox on
    assert len(offenders) == 1 and offenders[0].startswith("runtime/portal_mcp/login/local.py:"), offenders


# ----- F: tracking split ----------------------------------------------------

@pytest.mark.parametrize("host,tracking", [("www.googletagmanager.com", True), ("js.monitor.azure.com", True),
                                           ("consent.cookiebot.com", True), ("cdn.example.com", False),
                                           ("notgoogletagmanager.com", False)])
def test_tracking_hosts(host, tracking):
    assert is_tracking_host(host) is tracking


# ----- G: HAR deleted after analysis ----------------------------------------

def test_analysis_deletes_the_har_unless_asked_to_keep_it(tmp_path, capsys):
    har = {"log": {"entries": [{
        "request": {"method": "GET", "url": f"https://{HOST}/api/items"},
        "response": {"status": 200, "content": {"mimeType": "application/json", "text": "{\"a\": 1}"}},
    }]}}
    path = tmp_path / "recording.har"
    path.write_text(json.dumps(har))
    analyze.main([str(path), "--host", HOST, "--keep-har"])
    assert path.exists()
    analyze.main([str(path), "--host", HOST])
    assert not path.exists()
    out = capsys.readouterr().out
    assert "/api/items" in out


def test_login_not_verified_message_counts_answers():
    observation = LoginObservation()
    from portal_mcp.login.local import CheckResult

    observation.record(CheckResult(status=401))
    observation.record(CheckResult(status=401))
    text = login_not_verified_message(_manifest(), observation, 10)
    assert "401 x2" in text
