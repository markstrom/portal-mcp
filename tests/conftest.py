from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from cryptography.fernet import Fernet

from portal_mcp.config import Paths
from portal_mcp.session_store import SessionStore
from tests.mock_service.app import create_app

KEY = Fernet.generate_key()  # test key: never the keyring
os.environ.setdefault("PORTAL_MCP_NO_NOTIFY", "1")  # no desktop notifications during tests


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def mock_base_url():
    """Runs the mock portal on a free localhost port for the duration of one test."""
    port = free_port()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock service did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(base=tmp_path / "home")
    p.ensure()
    return p


@pytest.fixture
def store(paths: Paths) -> SessionStore:
    return SessionStore(paths.sessions, key_provider=lambda: KEY)


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    path = tmp_path / "downloads"
    path.mkdir()
    return path


def write_manifest(services_dir: Path, name: str, text: str) -> Path:
    services_dir.mkdir(parents=True, exist_ok=True)
    target = services_dir / f"{name}.yaml"
    target.write_text(text, encoding="utf-8")
    return target


def mock_manifest_yaml(base_url: str, *, rate: int = 30, cache: int = 0, extra_tools: str = "") -> str:
    return f"""
name: mockportal
enabled: true
base_url: {base_url}
login_url: {base_url}/login
profiles: [default, second]
session:
  type: cookies
  csrf:
    header: x-csrf-token
    from_cookie: csrf
  check: /api/session
  logout: /api/logout
rate_limit_per_minute: {rate}
cache_seconds: {cache}
redact: [items.owner.national_id, items.owner.address]
tools:
  - name: list_invoices
    kind: read
    description: List invoices with date, amount and status.
    method: GET
    path: /api/invoices
    params:
      type: object
      properties:
        page: {{type: integer}}
        page_size: {{type: integer}}
    pick: [items.id, items.date, items.amount, items.status, items.owner]
  - name: download_invoice_pdf
    kind: file
    description: Download the PDF for one invoice.
    method: GET
    path: /api/invoices/{{invoice_id}}/pdf
    params:
      type: object
      properties:
        invoice_id: {{type: string}}
      required: [invoice_id]
  - name: create_report
    kind: write
    description: Create a report.
    method: POST
    path: /api/reports
    body_schema:
      type: object
      properties:
        title: {{type: string}}
        text: {{type: string}}
      required: [title]
    limits:
      max_length: {{title: 80}}
    summary: "Create report: {{title}}"
  - name: pay
    kind: binding
    description: Pay an amount to a recipient.
    method: POST
    path: /api/payments
    body_encoding: form
    body_schema:
      type: object
      properties:
        amount: {{type: number}}
        recipient: {{type: string}}
      required: [amount, recipient]
    body_constants: {{currency: SEK}}
    limits:
      max: {{amount: 5000}}
      min: {{amount: 1}}
    summary: "Pay {{amount}} SEK to {{recipient}}"
  - name: send_message
    kind: write
    messaging: true
    description: Send a message to another person.
    method: POST
    path: /api/messages
    body_schema:
      type: object
      properties:
        to: {{type: string}}
        text: {{type: string}}
      required: [to, text]
{extra_tools}
"""
