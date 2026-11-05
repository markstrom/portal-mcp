"""Declared data hosts in recordings (step 1). Runtime tools still stay on the base_url host."""

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
import record  # noqa: E402

HOST = "app.example.com"
DATA = "api.example.net"
LOGIN = "login.example.com"


def _manifest(**extra) -> dict:
    data = {
        "name": "example",
        "base_url": f"https://{HOST}",
        "login_url": f"https://{HOST}/login",
        "session": {"check": "/api/session"},
    }
    data.update(extra)
    return data


# ----- manifest ---------------------------------------------------------------

def test_data_hosts_default_to_empty_and_accept_plain_hosts():
    assert Manifest.model_validate(_manifest()).data_hosts == []
    assert Manifest.model_validate(_manifest(data_hosts=[DATA])).data_hosts == [DATA]


@pytest.mark.parametrize(
    "hosts",
    [["https://api.example.net"], ["api.example.net/x"], ["api.example.net:443"], ["API.example.net"],
     ["10.0.0.1"], ["localhost"], [HOST], [DATA, DATA], ["nodot"]],
)
def test_invalid_data_hosts_are_rejected(hosts):
    with pytest.raises(ValueError):
        Manifest.model_validate(_manifest(data_hosts=hosts))


def test_a_host_cannot_be_both_login_and_data_host():
    with pytest.raises(ValueError, match="both a login host and a data host"):
        Manifest.model_validate(_manifest(login_hosts=[DATA], data_hosts=[DATA]))


@pytest.mark.parametrize("path", ["//api.example.net/items", "https://api.example.net/items"])
def test_tool_paths_cannot_leave_the_base_url_host(path):
    with pytest.raises(ValueError, match="stay on the base_url host|must start with '/'"):
        Manifest.model_validate(_manifest(tools=[{"name": "x", "kind": "read", "path": path}]))


# ----- guard ------------------------------------------------------------------

@pytest.mark.parametrize("logged_in", [False, True])
def test_declared_data_host_is_allowed_for_get(logged_in):
    decision = guard.decide("GET", f"https://{DATA}/v1/cases", HOST, logged_in, data_hosts=[DATA])
    assert decision.allowed and "data host" in decision.reason


def test_writes_to_a_data_host_are_captured_not_sent():
    url = f"https://{DATA}/v1/cases"
    after = guard.decide("POST", url, HOST, True, data_hosts=[DATA])
    assert not after.allowed and "captured" in after.reason
    assert guard.should_capture("POST", url, HOST, True, data_hosts=[DATA])
    # only with the user's explicit yes is it sent for real
    assert guard.decide("POST", url, HOST, True, allow_writes=True, data_hosts=[DATA]).allowed
    # before login: blocked and not captured
    assert not guard.decide("POST", url, HOST, False, data_hosts=[DATA]).allowed
    assert not guard.should_capture("POST", url, HOST, False, data_hosts=[DATA])


def test_data_host_needs_https():
    assert not guard.decide("GET", f"http://{DATA}/v1/cases", HOST, True, data_hosts=[DATA]).allowed


def test_unknown_host_is_blocked_as_not_declared():
    decision = guard.decide("GET", "https://cases.example.org/v1", HOST, True, data_hosts=[DATA])
    assert not decision.allowed and "not declared" in decision.reason
    assert not guard.should_capture("POST", "https://cases.example.org/v1", HOST, True, data_hosts=[DATA])


def test_behaviour_without_data_hosts_is_unchanged():
    assert not guard.decide("GET", f"https://{DATA}/v1/cases", HOST, True).allowed
    assert guard.decide("GET", f"https://{HOST}/api/items", HOST, True).allowed
    assert not guard.should_capture("POST", f"https://{DATA}/v1/cases", HOST, True)


# ----- the session never goes to a data host ------------------------------------

async def test_login_capture_reads_only_the_service_hosts_cookies():
    import httpx
    import yaml

    from portal_mcp.login.local import _capture

    manifest = Manifest.model_validate(yaml.safe_load(f"""
name: example
base_url: https://{HOST}
login_url: https://{HOST}/login
data_hosts: [{DATA}]
session: {{check: /api/session}}
"""))
    asked: list[list[str]] = []
    sent_to: list[str] = []

    class Ctx:
        async def cookies(self, urls):
            asked.append(list(urls))
            return [{"name": "sid", "value": "s", "domain": HOST, "path": "/"}]

    def handler(request):
        sent_to.append(request.url.host)
        return httpx.Response(200)

    session = await _capture(Ctx(), None, manifest, transport=httpx.MockTransport(handler))
    assert session is not None
    assert asked == [[f"https://{HOST}"]]
    assert sent_to == [HOST]


async def test_runtime_tools_only_call_the_base_url_host(tmp_path):
    import httpx

    from portal_mcp.session_store import StoredSession
    from portal_mcp.service import ServiceClient

    manifest = Manifest.model_validate(_manifest(
        data_hosts=[DATA], tools=[{"name": "items", "kind": "read", "path": "/api/items"}],
    ))
    client = ServiceClient(manifest, store=None, logins=None, downloads=tmp_path)
    http = client._http(StoredSession(cookies=[{"name": "sid", "value": "s", "domain": HOST, "path": "/"}]))
    assert http.base_url.host == HOST
    await http.aclose()


# ----- recorder auth observation ----------------------------------------------

def test_auth_observation_keeps_names_and_schemes_never_values():
    stats: dict = {}
    record.observe_auth(stats, {"authorization": "Bearer eyJSECRET", "cookie": "a=SECRET2", "x-api-key": "SECRET3"})
    record.observe_auth(stats, {"accept": "application/json"})
    assert stats == {
        "requests": 2,
        "authorization_schemes": {"Bearer": 1},
        "with_cookies": 1,
        "auth_headers": ["x-api-key"],
    }
    assert "SECRET" not in json.dumps(stats)


# ----- analyzer ---------------------------------------------------------------

def _entry(url: str, method: str = "GET", mime: str = "application/json", body: str = "{}"):
    return {"request": {"method": method, "url": url},
            "response": {"status": 200, "content": {"mimeType": mime, "text": body}}}


def test_analysis_includes_declared_data_hosts_only():
    har = {"log": {"entries": [
        _entry(f"https://{HOST}/api/items", body='{"a": 1}'),
        _entry(f"https://{DATA}/v1/cases", body='{"items": []}'),
        _entry("https://other.example.org/v1/x"),
    ]}}
    without = analyze.analyze(har, HOST)
    assert [t["host"] for t in without["tools"]] == [HOST]
    with_data = analyze.analyze(har, HOST, [DATA])
    hosts = [(t["host"], t["data_host"]) for t in with_data["tools"]]
    assert hosts == [(HOST, False), (DATA, True)]  # the service's own host first
    assert with_data["skipped"]["other host"] == 1


def test_write_candidates_show_their_host():
    captured = [
        {"host": DATA, "method": "POST", "path": "/v1/cases", "content_type": "application/json", "body": '{"title": "x"}'},
        {"method": "POST", "path": "/api/reports", "content_type": "application/json", "body": '{"t": 1}'},
    ]
    writes = {(w["host"], w["path"]): w for w in analyze.analyze_writes(captured, HOST)}
    assert writes[(DATA, "/v1/cases")]["data_host"] is True
    assert writes[(HOST, "/api/reports")]["data_host"] is False  # older captures without host
