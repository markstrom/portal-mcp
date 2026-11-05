"""Data hosts at runtime, CSRF tokens from pages and in bodies, uploads, public tools, file size."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from portal_mcp.login.local import token_from_value
from portal_mcp.manifest import Manifest
from portal_mcp.service import MAX_FILE_BYTES, ServiceClient, ServiceError, csrf_token_in
from portal_mcp.session_store import StoredSession

HOST = "portal.example.com"
DATA = "api.example.net"


def manifest(**extra) -> dict:
    data = {
        "name": "portal", "base_url": f"https://{HOST}", "login_url": f"https://{HOST}/login",
        "session": {"type": "cookies", "check": "/api/session"},
        "tools": [{"name": "info", "kind": "read", "description": "Info.", "path": "/api/info"}],
    }
    data.update(extra)
    return data


DATA_AUTH = {"source": "sessionStorage:oidc.user:*", "field": "access_token", "page": "/app"}
DATA_TOOL = {"name": "cases", "kind": "read", "description": "Cases.", "path": "/api/cases", "host": DATA}


def test_data_host_tools_need_a_declared_host_and_data_auth():
    Manifest.model_validate(manifest(data_hosts=[DATA], data_auth=DATA_AUTH, tools=[DATA_TOOL]))
    with pytest.raises(ValidationError, match="not a declared data host"):
        Manifest.model_validate(manifest(data_hosts=["other.example.net"], data_auth=DATA_AUTH, tools=[DATA_TOOL]))
    with pytest.raises(ValidationError, match="needs data_auth"):
        Manifest.model_validate(manifest(data_hosts=[DATA], tools=[DATA_TOOL]))
    with pytest.raises(ValidationError, match="data_auth needs data_hosts"):
        Manifest.model_validate(manifest(data_auth=DATA_AUTH))
    with pytest.raises(ValidationError, match="sessionStorage"):
        Manifest.model_validate(manifest(data_hosts=[DATA], data_auth={**DATA_AUTH, "source": "cookie:x"}))


def test_the_token_is_read_from_a_json_field():
    assert token_from_value('{"access_token": "t1", "token_type": "Bearer"}', "access_token") == "t1"
    assert token_from_value('{"a": {"b": "t2"}}', "a.b") == "t2"
    assert token_from_value("raw-token", None) == "raw-token"
    assert token_from_value("not json", "access_token") is None
    assert token_from_value(None, "access_token") is None


def client_for(data: dict, tmp_path, **kwargs) -> ServiceClient:
    return ServiceClient(Manifest.model_validate(data), store=None, logins=None, downloads=tmp_path, **kwargs)


async def test_a_data_host_gets_the_token_and_never_the_cookies(tmp_path):
    client = client_for(manifest(data_hosts=[DATA], data_auth=DATA_AUTH, tools=[DATA_TOOL]), tmp_path)
    session = StoredSession(cookies=[{"name": "sid", "value": "secret", "domain": HOST, "path": "/"}],
                            csrf="c", data_token="app-token")
    http = client._http(session, client.tool("cases"))
    assert str(http.base_url) == f"https://{DATA}"
    assert http.headers["authorization"] == "Bearer app-token"
    assert not http.cookies and "x-csrf-token" not in http.headers
    await http.aclose()

    with pytest.raises(ServiceError, match="no token"):
        client._http(StoredSession(), client.tool("cases"))


def with_transport(client: ServiceClient, handler, monkeypatch):
    original = client._http

    def patched(session, tool=None):
        real = original(session, tool)
        return httpx.AsyncClient(base_url=real.base_url, headers=real.headers, cookies=real.cookies,
                                 transport=httpx.MockTransport(handler))

    monkeypatch.setattr(client, "_http", patched)


def test_csrf_tokens_are_found_in_inputs_and_meta_tags():
    page = '<input type="hidden" name="__token" value="a&amp;b"><meta name="csrf" content="m">'
    assert csrf_token_in(page, "__token") == "a&b"
    assert csrf_token_in(page, "csrf") == "m"
    assert csrf_token_in(page, "missing") is None


def test_csrf_config_needs_one_source_and_a_target():
    session = {"type": "cookies", "check": "/api/session"}
    with pytest.raises(ValidationError, match="exactly one"):
        Manifest.model_validate(manifest(session={**session, "csrf": {"header": "x"}}))
    with pytest.raises(ValidationError, match="header or a body_field"):
        Manifest.model_validate(manifest(session={**session, "csrf": {"from_cookie": "c"}}))


WRITE = {"name": "send_form", "kind": "write", "description": "Send.", "method": "POST", "path": "/api/form",
         "body_schema": {"type": "object", "properties": {"text": {"type": "string"}}}, "body_encoding": "form"}


async def test_a_csrf_token_from_a_page_goes_into_the_body(tmp_path, monkeypatch):
    session_cfg = {"type": "cookies", "check": "/api/session",
                   "csrf": {"from_page": {"path": "/form", "name": "__token"}, "body_field": "__token"}}
    client = client_for(manifest(session=session_cfg, tools=[WRITE]), tmp_path)
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/form":
            return httpx.Response(200, text='<input name="__token" value="fresh-1">')
        return httpx.Response(200, json={"ok": True})

    with_transport(client, handler, monkeypatch)
    response = await client._send(client.tool("send_form"), {"text": "hi"}, StoredSession())
    assert response.status_code == 200
    assert seen[0].url.path == "/form"
    assert b"__token=fresh-1" in seen[1].content and b"text=hi" in seen[1].content


UPLOAD = {"name": "upload", "kind": "write", "description": "Upload.", "method": "POST", "path": "/api/upload",
          "body_schema": {"type": "object", "properties": {"file": {"type": "string"}, "title": {"type": "string"}}},
          "body_encoding": "multipart", "file_fields": ["file"]}


async def test_uploads_come_only_from_the_uploads_directory(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "photo.jpg").write_bytes(b"jpeg-bytes")
    (tmp_path / "secret.txt").write_text("not for upload")
    client = client_for(manifest(tools=[UPLOAD]), tmp_path, uploads=uploads)

    assert client.upload_path("photo.jpg") == (uploads / "photo.jpg").resolve()
    for outside in ("../secret.txt", str(tmp_path / "secret.txt"), "/etc/hosts", "missing.jpg"):
        with pytest.raises(ServiceError):
            client.upload_path(outside)

    seen = []
    with_transport(client, lambda r: seen.append(r) or httpx.Response(201, json={}), monkeypatch)
    await client._send(client.tool("upload"), {"file": "photo.jpg", "title": "T"}, StoredSession())
    body = seen[0].content
    assert b'filename="photo.jpg"' in body and b"jpeg-bytes" in body and b"T" in body
    assert seen[0].headers["content-type"].startswith("multipart/form-data")


def test_file_fields_need_multipart_and_public_writes_are_refused():
    with pytest.raises(ValidationError, match="multipart"):
        Manifest.model_validate(manifest(tools=[{**UPLOAD, "body_encoding": "form"}]))
    with pytest.raises(ValidationError, match="cannot be public"):
        Manifest.model_validate(manifest(tools=[{**WRITE, "public": True}]))


class NoLogins:
    def ensure(self, *args, **kwargs):
        raise AssertionError("a public tool must not start a login")


async def test_a_public_tool_in_a_logged_in_service_needs_no_login(tmp_path, monkeypatch):
    tools = [{"name": "opening_hours", "kind": "read", "description": "Hours.", "path": "/api/hours", "public": True}]
    client = ServiceClient(Manifest.model_validate(manifest(tools=tools)), store=None, logins=NoLogins(),
                           downloads=tmp_path)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"open": "8-17"})

    with_transport(client, handler, monkeypatch)
    assert await client.call("opening_hours", {}) == {"open": "8-17"}
    assert "cookie" not in seen[0].headers


async def test_file_tools_may_be_larger_than_reads(tmp_path, monkeypatch):
    tools = [{"name": "big", "kind": "file", "description": "Big.", "path": "/big", "public": True},
             {"name": "data", "kind": "read", "description": "Data.", "path": "/data", "public": True}]
    client = client_for(manifest(tools=tools), tmp_path)
    eleven_mb = b"x" * (11 * 1024 * 1024)
    with_transport(client, lambda r: httpx.Response(200, content=eleven_mb,
                                                    headers={"content-type": "application/pdf"}), monkeypatch)
    assert MAX_FILE_BYTES > len(eleven_mb)
    assert (await client._send(client.tool("big"), {}, None)).status_code == 200
    with pytest.raises(ServiceError, match="larger than"):
        await client._send(client.tool("data"), {}, None)


def test_the_analyzer_reads_multipart_writes_without_file_content():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"))
    import analyze

    body = ("--XyZ\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nBroken window\r\n"
            "--XyZ\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"me.jpg\"\r\n"
            "Content-Type: image/jpeg\r\n\r\nPRIVATE-BYTES\r\n--XyZ--\r\n")
    writes = analyze.analyze_writes([{"method": "POST", "path": "/api/report", "body": body,
                                      "content_type": "multipart/form-data; boundary=XyZ"}], HOST)
    assert writes[0]["encoding"] == "multipart"
    assert writes[0]["fields"] == {"title": "string", "photo": "file"}
    assert writes[0]["file_fields"] == ["photo"]
    assert "PRIVATE-BYTES" not in str(writes) and "Broken window" not in str(writes)
