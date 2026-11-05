"""Single-page apps with their own OIDC client: asset hosts (A), OIDC endpoints on a login
host after login (B), and refusing to analyze recordings without a verified login (C)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from portal_mcp.manifest import Manifest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import guard  # noqa: E402

HOST = "app.example.com"
LOGIN = "login.example.com"
CDN = "cdn.example.net"
DISCOVERY = {
    "issuer": f"https://{LOGIN}",
    "authorization_endpoint": f"https://{LOGIN}/connect/authorize",
    "token_endpoint": f"https://{LOGIN}/connect/token",
    "userinfo_endpoint": f"https://{LOGIN}/connect/userinfo",
    "jwks_uri": f"https://{LOGIN}/.well-known/openid-configuration/jwks",
    "end_session_endpoint": f"https://{LOGIN}/connect/endsession",
    "check_session_iframe": f"https://{LOGIN}/connect/checksession",
    "revocation_endpoint": f"https://elsewhere.example.org/revoke",
}


def _manifest(**extra) -> dict:
    data = {"name": "example", "base_url": f"https://{HOST}", "login_url": f"https://{HOST}/login",
            "session": {"check": "/api/session"}}
    data.update(extra)
    return data


# ----- A: asset hosts -------------------------------------------------------------

def test_asset_hosts_validation():
    assert Manifest.model_validate(_manifest(asset_hosts=[CDN])).asset_hosts == [CDN]
    for bad in (["https://cdn.example.net"], [HOST], ["www.googletagmanager.com"], ["127.0.0.1"]):
        with pytest.raises(ValueError):
            Manifest.model_validate(_manifest(asset_hosts=bad))
    with pytest.raises(ValueError, match="in both data_hosts and asset_hosts"):
        Manifest.model_validate(_manifest(data_hosts=[CDN], asset_hosts=[CDN]))


@pytest.mark.parametrize("resource_type", ["script", "stylesheet", "font", "image"])
def test_asset_host_serves_static_files(resource_type):
    decision = guard.decide("GET", f"https://{CDN}/lib.js", HOST, True, asset_hosts=[CDN], resource_type=resource_type)
    assert decision.allowed


@pytest.mark.parametrize(
    "method,resource_type,scheme",
    [("GET", "fetch", "https"), ("GET", "xhr", "https"), ("GET", "document", "https"),
     ("POST", "script", "https"), ("GET", "script", "http"), ("GET", None, "https")],
)
def test_asset_host_never_serves_data_navigation_or_writes(method, resource_type, scheme):
    decision = guard.decide(method, f"{scheme}://{CDN}/x", HOST, True, asset_hosts=[CDN], resource_type=resource_type)
    assert not decision.allowed
    assert not guard.should_capture(method, f"{scheme}://{CDN}/x", HOST, True)


def test_undeclared_cdn_stays_blocked():
    assert not guard.decide("GET", f"https://{CDN}/lib.js", HOST, True, resource_type="script").allowed


# ----- B: OIDC endpoints on a login host after login ------------------------------

def test_manifest_option_needs_a_login_host_and_defaults_to_blocked():
    assert Manifest.model_validate(_manifest()).login_hosts_after_login == "blocked"
    with pytest.raises(ValueError, match="needs at least one login host"):
        Manifest.model_validate(_manifest(login_hosts_after_login="oidc"))
    with pytest.raises(ValueError):
        Manifest.model_validate(_manifest(login_hosts=[LOGIN], login_hosts_after_login="open"))


def test_endpoints_come_from_discovery_on_the_same_host_without_logout():
    allowed = guard.oidc_endpoints_from(DISCOVERY, LOGIN)
    assert "/connect/authorize" in allowed["get"] and "/connect/userinfo" in allowed["get"]
    assert "/connect/checksession" in allowed["get"]
    assert "/.well-known/openid-configuration/jwks" in allowed["get"]
    assert allowed["post"] == {"/connect/token"}
    assert "/connect/endsession" not in allowed["get"] | allowed["post"]  # logout is never opened
    assert "/revoke" not in allowed["get"] | allowed["post"]  # other hosts are ignored


def _after_login(method, path, endpoints=None, mode="oidc"):
    return guard.decide(method, f"https://{LOGIN}{path}", HOST, True, login_hosts=[LOGIN],
                        login_after_login=mode, oidc_endpoints=endpoints)


def test_default_keeps_the_login_host_blocked_after_login():
    endpoints = {LOGIN: guard.oidc_endpoints_from(DISCOVERY, LOGIN)}
    assert not _after_login("GET", "/.well-known/openid-configuration", endpoints, mode="blocked").allowed
    assert not _after_login("GET", "/connect/authorize", endpoints, mode="blocked").allowed


def test_oidc_mode_opens_only_named_endpoints():
    endpoints = {LOGIN: guard.oidc_endpoints_from(DISCOVERY, LOGIN)}
    assert _after_login("GET", "/.well-known/openid-configuration").allowed  # discovery, even before it is known
    assert _after_login("GET", "/connect/authorize", endpoints).allowed
    assert _after_login("POST", "/connect/token", endpoints).allowed
    assert not _after_login("POST", "/connect/authorize", endpoints).allowed  # POST only to token
    assert not _after_login("GET", "/connect/endsession", endpoints).allowed
    assert not _after_login("GET", "/account/profile", endpoints).allowed
    assert not _after_login("GET", "/connect/authorize").allowed  # unknown until discovery is seen
    assert not guard.should_capture("POST", f"https://{LOGIN}/connect/token", HOST, True)  # never captured


def test_oidc_mode_needs_https():
    decision = guard.decide("GET", f"http://{LOGIN}/.well-known/openid-configuration", HOST, True,
                            login_hosts=[LOGIN], login_after_login="oidc")
    assert not decision.allowed


# ----- C: recordings without a verified login ---------------------------------------

def _recording(tmp_path: Path, verified: bool | None) -> Path:
    har = {"log": {"entries": [{
        "request": {"method": "GET", "url": f"https://{HOST}/api/items"},
        "response": {"status": 200, "content": {"mimeType": "application/json", "text": "{\"a\": 1}"}},
    }]}}
    path = tmp_path / "recording.har"
    path.write_text(json.dumps(har))
    if verified is not None:
        (tmp_path / "recording.json").write_text(json.dumps({"login_verified": verified}))
    return path


def test_unverified_recording_is_refused_and_kept(tmp_path, capsys):
    har = _recording(tmp_path, verified=False)
    assert analyze.main([str(har), "--host", HOST]) == 2
    assert har.exists()
    assert "not verified" in capsys.readouterr().err


def test_unverified_recording_can_be_analyzed_on_request_and_is_marked(tmp_path, capsys):
    har = _recording(tmp_path, verified=False)
    assert analyze.main([str(har), "--host", HOST, "--allow-unverified"]) == 0
    assert json.loads(capsys.readouterr().out)["logged_in"] is False


def test_verified_recording_is_marked_logged_in(tmp_path, capsys):
    har = _recording(tmp_path, verified=True)
    assert analyze.main([str(har), "--host", HOST]) == 0
    assert json.loads(capsys.readouterr().out)["logged_in"] is True


def test_recording_without_marker_warns_but_is_analyzed(tmp_path, capsys):
    har = _recording(tmp_path, verified=None)
    assert analyze.main([str(har), "--host", HOST]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["logged_in"] is None
    assert "cannot tell whether the login was verified" in captured.err
