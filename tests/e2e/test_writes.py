"""Write and binding tools: prepare/confirm, human approval, limits, one-time codes and the audit log."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import stat

import httpx
import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from portal_mcp.login.base import LoginManager
from portal_mcp.registry import Registry
from portal_mcp.server import build_server
from portal_mcp.writes import BrowserApproval, PendingActions
from tests.conftest import mock_manifest_yaml, write_manifest
from tests.e2e.test_runtime import FakeLogin, _jar


def _text(result) -> str:
    return result.content[0].text


def _json(result):
    return json.loads(_text(result))


class Harness:
    def __init__(self, paths, store, downloads, base_url, *, wait_seconds=5.0, clock=None, manifest=None):
        self.opened: list[str] = []
        self.base = base_url
        self.paths = paths
        self.store = store
        self.provider = FakeLogin()
        self.pending = PendingActions(clock=clock) if clock else PendingActions()
        self.approval = BrowserApproval(self.pending, opener=self.opened.append, wait_seconds=wait_seconds)
        write_manifest(paths.services, "mockportal", manifest or mock_manifest_yaml(base_url))
        self.registry = Registry(
            paths, store, LoginManager(self.provider, store), downloads,
            pending=self.pending, approval=self.approval,
        )
        self.server, _ = build_server(self.registry)

    async def wait_for_page(self, count: int = 1) -> str:
        for _ in range(500):
            if len(self.opened) >= count:
                return self.opened[count - 1]
            await asyncio.sleep(0.01)
        raise AssertionError("confirmation page was never opened")

    async def click(self, url: str, decision: str, origin: str | None = "same") -> httpx.Response:
        headers = {}
        if origin == "same":
            headers["origin"] = self.approval.origin
        elif origin is not None:
            headers["origin"] = origin
        async with httpx.AsyncClient() as client:
            return await client.post(url, data={"decision": decision}, headers=headers)

    async def payments(self) -> list[dict]:
        session = self.store.load("mockportal", "default")
        async with httpx.AsyncClient(base_url=self.base, cookies=_jar(session)) as client:
            return (await client.get("/api/payments")).json()["items"]

    def audit_events(self) -> list[str]:
        if not self.paths.audit_log.exists():
            return []
        return [json.loads(line)["event"] for line in self.paths.audit_log.read_text().splitlines()]


@pytest.fixture
async def h(paths, store, downloads, mock_base_url):
    harness = Harness(paths, store, downloads, mock_base_url)
    yield harness
    await harness.approval.close()


async def test_write_tools_are_exposed_as_prepare_and_confirm(h):
    async with create_connected_server_and_client_session(h.server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        status = _json(await client.call_tool("list_services", {}))
    assert "mockportal_prepare_create_report" in tools
    assert "mockportal_confirm_create_report" in tools
    assert "mockportal_prepare_pay" in tools
    assert tools["mockportal_confirm_pay"].annotations.destructiveHint is True
    assert tools["mockportal_prepare_pay"].annotations.readOnlyHint is True
    # messaging tools stay off until the manifest enables them
    assert "mockportal_prepare_send_message" not in tools
    assert "send_message" in status["services"]["mockportal"]["hidden_tools"]
    schema = tools["mockportal_prepare_pay"].inputSchema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"amount", "recipient"}


async def test_messaging_tool_can_be_enabled_explicitly(paths, store, downloads, mock_base_url):
    manifest = mock_manifest_yaml(mock_base_url).replace("    messaging: true\n", "    messaging: true\n    enabled: true\n")
    harness = Harness(paths, store, downloads, mock_base_url, manifest=manifest)
    async with create_connected_server_and_client_session(harness.server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "mockportal_prepare_send_message" in names


@pytest.mark.parametrize(
    "tool,args,message",
    [
        ("pay", {"amount": 6000, "recipient": "Testmottagare"}, "above the limit"),
        ("pay", {"amount": 0, "recipient": "Testmottagare"}, "below the limit"),
        ("pay", {"amount": 100}, "required"),
        ("pay", {"amount": 100, "recipient": "x", "iban": "extra"}, "Additional properties"),
        ("create_report", {"title": "x" * 81}, "longer than 80"),
    ],
)
async def test_prepare_refuses_invalid_input_and_limits(h, tool, args, message):
    async with create_connected_server_and_client_session(h.server) as client:
        result = await client.call_tool(f"mockportal_prepare_{tool}", args)
    assert result.isError
    assert message in _text(result)
    assert h.audit_events() == ["refused"]
    assert h.provider.calls == 0  # nothing was sent, not even a login


async def test_browser_approval_runs_the_action_once(h):
    async with create_connected_server_and_client_session(h.server) as client:
        prepared = _json(await client.call_tool("mockportal_prepare_pay", {"amount": 250, "recipient": "Testmottagare"}))
        assert prepared["status"] == "prepared"
        assert "Pay 250 SEK to Testmottagare" in prepared["summary"]
        code = prepared["code"]

        confirm = asyncio.create_task(client.call_tool("mockportal_confirm_pay", {"code": code}))
        url = await h.wait_for_page()
        assert code not in url and "confirm/" in url  # the page token is not the code

        async with httpx.AsyncClient() as raw:
            page = await raw.get(url)
        assert page.status_code == 200
        assert "Testmottagare" in page.text and "Bekräfta" in page.text
        assert (await h.click(url, "approve")).status_code == 200

        done = _json(await confirm)
        assert done["status"] == "done"
        assert done["http_status"] == 201

        again = await client.call_tool("mockportal_confirm_pay", {"code": code})
    assert again.isError and "unknown or expired" in _text(again)

    payments = await h.payments()
    assert len(payments) == 1
    assert payments[0]["amount"] == "250" and payments[0]["currency"] == "SEK"  # form-encoded, constant added
    assert h.audit_events() == ["prepared", "approved", "executed"]


async def test_decline_sends_nothing(h):
    async with create_connected_server_and_client_session(h.server) as client:
        code = _json(await client.call_tool("mockportal_prepare_pay", {"amount": 10, "recipient": "R"}))["code"]
        confirm = asyncio.create_task(client.call_tool("mockportal_confirm_pay", {"code": code}))
        await h.click(await h.wait_for_page(), "decline")
        result = _json(await confirm)
    assert result["status"] == "declined"
    assert h.provider.calls == 0  # never even logged in
    assert h.audit_events() == ["prepared", "declined"]


async def test_confirm_returns_while_waiting_and_works_on_the_next_call(paths, store, downloads, mock_base_url):
    harness = Harness(paths, store, downloads, mock_base_url, wait_seconds=0.2)
    try:
        async with create_connected_server_and_client_session(harness.server) as client:
            code = _json(await client.call_tool("mockportal_prepare_create_report", {"title": "Test"}))["code"]
            waiting = _json(await client.call_tool("mockportal_confirm_create_report", {"code": code}))
            assert waiting["status"] == "waiting_for_approval"

            await harness.click(harness.opened[0], "approve")
            done = _json(await client.call_tool("mockportal_confirm_create_report", {"code": code}))
        assert done["status"] == "done"
        assert len(harness.opened) == 1  # the page is opened once per action
    finally:
        await harness.approval.close()


async def test_post_from_another_origin_is_rejected(h):
    async with create_connected_server_and_client_session(h.server) as client:
        code = _json(await client.call_tool("mockportal_prepare_pay", {"amount": 10, "recipient": "R"}))["code"]
        confirm = asyncio.create_task(client.call_tool("mockportal_confirm_pay", {"code": code}))
        url = await h.wait_for_page()
        assert (await h.click(url, "approve", origin="http://evil.example")).status_code == 403
        await h.click(url, "decline")
        assert _json(await confirm)["status"] == "declined"


async def test_code_expires_after_five_minutes(paths, store, downloads, mock_base_url):
    now = [0.0]
    harness = Harness(paths, store, downloads, mock_base_url, clock=lambda: now[0])
    try:
        async with create_connected_server_and_client_session(harness.server) as client:
            code = _json(await client.call_tool("mockportal_prepare_create_report", {"title": "T"}))["code"]
            now[0] = 301.0
            result = await client.call_tool("mockportal_confirm_create_report", {"code": code})
        assert result.isError and "expired" in _text(result)
    finally:
        await harness.approval.close()


async def test_code_only_works_for_its_own_tool(h):
    async with create_connected_server_and_client_session(h.server) as client:
        code = _json(await client.call_tool("mockportal_prepare_create_report", {"title": "T"}))["code"]
        result = await client.call_tool("mockportal_confirm_pay", {"code": code})
    assert result.isError and "another tool" in _text(result)


async def test_elicitation_is_used_when_the_client_supports_it(h):
    asked: list[str] = []

    async def accept(context, params):
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"confirm": True})

    async with create_connected_server_and_client_session(h.server, elicitation_callback=accept) as client:
        code = _json(await client.call_tool("mockportal_prepare_pay", {"amount": 99, "recipient": "R"}))["code"]
        done = _json(await client.call_tool("mockportal_confirm_pay", {"code": code}))
    assert done["status"] == "done"
    assert h.opened == []  # no browser page needed
    assert "Pay 99 SEK to R" in asked[0]


@pytest.mark.parametrize(
    "answer",
    [
        types.ElicitResult(action="decline"),
        types.ElicitResult(action="cancel"),
        types.ElicitResult(action="accept", content={"confirm": False}),
    ],
)
async def test_elicitation_without_explicit_yes_declines(h, answer):
    async def respond(context, params):
        return answer

    async with create_connected_server_and_client_session(h.server, elicitation_callback=respond) as client:
        code = _json(await client.call_tool("mockportal_prepare_pay", {"amount": 5, "recipient": "R"}))["code"]
        result = _json(await client.call_tool("mockportal_confirm_pay", {"code": code}))
    assert result["status"] == "declined"
    assert h.provider.calls == 0


async def test_limits_are_checked_again_at_confirm(h):
    async with create_connected_server_and_client_session(h.server) as client:
        code = _json(await client.call_tool("mockportal_prepare_pay", {"amount": 400, "recipient": "R"}))["code"]
        # The user lowers the limit in the manifest before the action is confirmed.
        path = h.paths.services / "mockportal.yaml"
        path.write_text(path.read_text().replace("max: {amount: 5000}", "max: {amount: 300}"))
        h.registry.reload()
        result = await client.call_tool("mockportal_confirm_pay", {"code": code})
    assert result.isError and "above the limit of 300" in _text(result)
    assert h.opened == []


async def test_audit_log_is_private_and_records_call_and_response(h):
    async def accept(context, params):
        return types.ElicitResult(action="accept", content={"confirm": True})

    async with create_connected_server_and_client_session(h.server, elicitation_callback=accept) as client:
        code = _json(await client.call_tool("mockportal_prepare_create_report", {"title": "Rapport"}))["code"]
        await client.call_tool("mockportal_confirm_create_report", {"code": code})
    if sys.platform != "win32":  # POSIX file modes; Windows protects the user profile by ACLs
        assert stat.S_IMODE(os.stat(h.paths.audit_log).st_mode) == 0o600
    executed = [json.loads(line) for line in h.paths.audit_log.read_text().splitlines()][-1]
    assert executed["event"] == "executed"
    assert executed["service"] == "mockportal" and executed["tool"] == "create_report"
    assert executed["call"] == {"title": "Rapport"}
    assert executed["response"]["http_status"] == 201
    assert "time" in executed
