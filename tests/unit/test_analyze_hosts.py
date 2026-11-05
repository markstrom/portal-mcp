"""analyze.py over every declared host (service host and data hosts), never login hosts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402

HOST = "app.example.com"
DATA = "api.example.net"
EMPTY = "files.example.net"
LOGIN = "login.example.com"


def _entry(url: str, method: str = "GET", status: int = 200, mime: str = "application/json", body: str = "{}"):
    return {"request": {"method": method, "url": url},
            "response": {"status": status, "content": {"mimeType": mime, "text": body}}}


HAR = {"log": {"entries": [
    _entry(f"https://{HOST}/api/items", body=json.dumps({"items": [{"id": 1, "name": "SECRET-NAME"}]})),
    _entry(f"https://{DATA}/v1/cases", body=json.dumps({"cases": [{"case_id": "c1", "owner_email": "SECRET@x"}]})),
    _entry(f"https://{DATA}/v1/cases/123", body=json.dumps({"status": "open"})),
    _entry(f"https://{LOGIN}/connect/userinfo", body=json.dumps({"sub": "SECRET-SUB", "email": "SECRET@y"})),
    _entry(f"https://{EMPTY}/logo.png", mime="image/png", body=""),
    _entry("https://tracker.example.org/collect", method="POST"),
]}}


def _manifest(tmp_path: Path, data_hosts=(DATA,), login_hosts=(LOGIN,)) -> Path:
    path = tmp_path / "example.yaml"
    lines = [
        "name: example",
        f"base_url: https://{HOST}",
        f"login_url: https://{HOST}/login",
        f"login_hosts: [{', '.join(login_hosts)}]",
        f"data_hosts: [{', '.join(data_hosts)}]",
        "session: {check: /api/session}",
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_two_hosts_give_tools_from_both_marked_with_their_host():
    catalog = analyze.analyze_hosts(HAR, HOST, [DATA], [LOGIN])
    by_host = {}
    for tool in catalog["tools"]:
        by_host.setdefault(tool["host"], []).append(tool["path"])
    assert by_host == {HOST: ["/api/items"], DATA: ["/v1/cases", "/v1/cases/{case_id}"]}
    assert all(t["data_host"] is (t["host"] == DATA) for t in catalog["tools"])
    reports = {r["host"]: r for r in catalog["hosts"]}
    assert reports[HOST]["status"] == "ok" and reports[HOST]["role"] == "service"
    assert reports[DATA]["status"] == "ok" and reports[DATA]["tools_found"] == 2
    assert catalog["failed_hosts"] == []
    dumped = json.dumps(catalog)
    assert "SECRET" not in dumped  # field names and types only, for every host
    assert ["cases.owner_email"] in [t["sensitive_fields"] for t in catalog["tools"]]


def test_login_hosts_give_no_tools():
    catalog = analyze.analyze_hosts(HAR, HOST, [DATA], [LOGIN])
    assert not any(t["host"] == LOGIN for t in catalog["tools"])
    login = {r["host"]: r for r in catalog["hosts"]}[LOGIN]
    assert login["role"] == "login" and login["status"] == "not analyzed" and login["requests"] == 1
    assert catalog["skipped"]["login host (never a data source)"] == 1
    assert "userinfo" not in json.dumps(catalog["tools"])


def test_a_host_without_json_reads_is_reported_as_empty_with_a_reason():
    catalog = analyze.analyze_hosts(HAR, HOST, [DATA, EMPTY, "quiet.example.net"], [LOGIN])
    reports = {r["host"]: r for r in catalog["hosts"]}
    assert reports[EMPTY]["status"] == "empty" and "no JSON GET answers" in reports[EMPTY]["reason"]
    assert reports["quiet.example.net"]["status"] == "empty"
    assert "no requests to this host" in reports["quiet.example.net"]["reason"]


def test_failed_host_keeps_the_har_and_is_named(tmp_path, monkeypatch, capsys):
    har_path = tmp_path / "recording.har"
    har_path.write_text(json.dumps(HAR))
    original = analyze.analyze

    def flaky(har, host, data_hosts=()):
        if host == DATA:
            raise ValueError("broken entry")
        return original(har, host, data_hosts)

    monkeypatch.setattr(analyze, "analyze", flaky)
    code = analyze.main([str(har_path), "--manifest", str(_manifest(tmp_path))])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert har_path.exists()
    assert out["failed_hosts"] == [DATA]
    report = {r["host"]: r for r in out["hosts"]}[DATA]
    assert report["status"] == "failed" and report["error"] == "ValueError"
    assert any(t["host"] == HOST for t in out["tools"])  # the other host still came through


def test_manifest_run_deletes_the_har_when_every_host_succeeded(tmp_path, capsys):
    har_path = tmp_path / "recording.har"
    har_path.write_text(json.dumps(HAR))
    assert analyze.main([str(har_path), "--manifest", str(_manifest(tmp_path))]) == 0
    assert not har_path.exists()
    out = json.loads(capsys.readouterr().out)
    assert [r["host"] for r in out["hosts"]] == [HOST, DATA, LOGIN]


def test_single_host_behaviour_is_unchanged(tmp_path, capsys):
    har_path = tmp_path / "recording.har"
    har_path.write_text(json.dumps(HAR))
    assert analyze.main([str(har_path), "--host", HOST]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "hosts" not in out
    assert [t["path"] for t in out["tools"]] == ["/api/items"]
    assert not har_path.exists()


def test_host_or_manifest_is_required(tmp_path):
    har_path = tmp_path / "recording.har"
    har_path.write_text("{}")
    with pytest.raises(SystemExit):
        analyze.main([str(har_path)])
