"""Tests for the portal-mcp-creator helper scripts (pure logic, no network or browser)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import guard  # noqa: E402
import recon  # noqa: E402

HOST = "app.example.com"


# ----- guard ----------------------------------------------------------------

@pytest.mark.parametrize(
    "method,url,logged_in,allowed",
    [
        ("POST", f"https://{HOST}/login", False, True),          # login may post before login
        ("GET", f"https://{HOST}/api/invoices", True, True),
        ("POST", f"https://{HOST}/api/invoices", True, False),    # writes blocked after login
        ("GET", f"https://{HOST}/api/logout", True, False),      # side-effect GET
        ("GET", f"https://{HOST}/api/invoices/1/delete", True, False),
        ("GET", f"https://{HOST}/api/notifications/mark-as-read", True, False),
        ("GET", "https://cdn.other.com/lib.js", False, False),   # other host
        ("GET", "https://cdn.other.com/lib.js", True, False),
        ("GET", f"https://{HOST}/api/orders", True, True),       # "orders" is not "order"
    ],
)
def test_guard_decisions(method, url, logged_in, allowed):
    assert guard.decide(method, url, HOST, logged_in).allowed is allowed


def test_guard_reports_reason():
    decision = guard.decide("GET", f"https://{HOST}/api/logout", HOST, True)
    assert "side effect" in decision.reason and "logout" in decision.reason


# ----- analyze --------------------------------------------------------------

def _entry(url: str, method: str = "GET", status: int = 200, mime: str = "application/json", body: str = "{}"):
    return {
        "request": {"method": method, "url": url},
        "response": {"status": status, "content": {"mimeType": mime, "text": body}},
    }


def test_path_templates_and_schema_without_values():
    har = {"log": {"entries": [
        _entry(f"https://{HOST}/api/invoices/123/lines?page=1&pageSize=20",
               body=json.dumps({"items": [{"id": 1, "owner": {"national_id": "SECRET"}}], "total": 3})),
        _entry(f"https://{HOST}/api/invoices/456/lines?page=2",
               body=json.dumps({"items": [], "total": 3})),
        _entry(f"https://{HOST}/api/invoices/789/pdf", mime="application/pdf", body=""),
        _entry(f"https://{HOST}/static/app.js", mime="application/javascript", body="x"),
        _entry(f"https://{HOST}/api/logout"),
        _entry(f"https://{HOST}/api/invoices", method="POST"),
        _entry(f"https://other.example.org/api/x"),
    ]}}
    catalog = analyze.analyze(har, HOST)
    tools = {t["path"]: t for t in catalog["tools"]}

    lines = tools["/api/invoices/{invoice_id}/lines"]
    assert lines["kind"] == "read"
    assert lines["query_params"] == ["page", "pageSize"]
    assert lines["pagination_params"] == ["page", "pageSize"]
    assert lines["sensitive_fields"] == ["items.owner.national_id"]
    assert "SECRET" not in json.dumps(catalog)  # values never reach the catalog

    assert tools["/api/invoices/{invoice_id}/pdf"]["kind"] == "file"
    assert "/api/logout" not in tools
    assert catalog["skipped"]["side effect: logout"] == 1
    assert catalog["skipped"]["not GET"] == 1
    assert catalog["skipped"]["other host"] == 1


def test_suggested_names_are_tool_safe():
    assert analyze.suggest_name("/api/invoices/{invoice_id}/lines") == "api_invoices_invoice_id_lines"
    assert analyze.path_template("/v1/users/550e8400-e29b-41d4-a716-446655440000") == "/v1/users/{user_id}"


# ----- recon ----------------------------------------------------------------

def test_endpoint_extraction_from_javascript():
    source = """
      fetch("/api/invoices?page=1");
      axios.post("/api/reports", {data: 1});
      client.get('/api/customers/' + id);
      const url = "/rest/v2/balances";
      fetch("/api/save", { method: "PUT", body: "{}" });
      const unrelated = "/images/logo.png";
    """
    found = {(e["method"], e["path"]) for e in recon.extract_endpoints(source)}
    assert ("GET", "/api/invoices?page=1") in found
    assert ("POST", "/api/reports") in found
    assert ("GET", "/api/customers/") in found
    assert ("?", "/rest/v2/balances") in found
    assert ("PUT", "/api/save") in found
    assert not any("logo.png" in path for _, path in found)
