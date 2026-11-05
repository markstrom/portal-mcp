"""Builds the tool list from manifests and routes tool calls to the right service.

Each service is isolated: a broken manifest is recorded in `errors` and the rest keep working.

Read and file tools are exposed as `<service>_<tool>`. Write and binding tools are exposed
as two tools, `<service>_prepare_<tool>` and `<service>_confirm_<tool>`; see writes.py.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from mcp import types

from .config import Paths, has_display
from .login.base import LoginManager
from .manifest import load_manifests
from .service import ServiceClient, ServiceError
from .session_store import SessionStore
from .writes import (
    AuditLog,
    BrowserApproval,
    PendingActions,
    WriteError,
    build_summary,
    client_supports_elicitation,
    elicit_approval,
    validate_write,
    write_input_schema,
)

log = logging.getLogger("portal_mcp.registry")

COMMON_TOOLS = {"list_services", "forget_session", "logout"}

Role = Literal["call", "prepare", "confirm"]


@dataclass
class CallContext:
    """What the server knows about the current request. Used to ask the user for approval."""

    session: Any = None
    request_id: Any = None


class Registry:
    def __init__(
        self,
        paths: Paths,
        store: SessionStore,
        logins: LoginManager,
        downloads,
        *,
        pending: PendingActions | None = None,
        approval: BrowserApproval | None = None,
        audit: AuditLog | None = None,
    ):
        self._paths = paths
        self._store = store
        self._logins = logins
        self._downloads = downloads
        self.pending = pending or PendingActions()
        self.approval = approval or BrowserApproval(self.pending)
        self.audit = audit or AuditLog(paths.audit_log)
        self._clients: dict[str, ServiceClient] = {}
        self._tools: dict[str, tuple[str, str, Role]] = {}  # exposed name -> (service, tool, role)
        self.hidden: dict[str, str] = {}  # "<service>.<tool>" -> why it is not exposed
        self.errors: dict[str, str] = {}
        self.reload()

    @property
    def services_dir(self):
        return self._paths.services

    def reload(self) -> bool:
        """Re-read all manifests. Returns True if the set of exposed tools changed."""
        result = load_manifests(self._paths.services)
        clients: dict[str, ServiceClient] = {}
        errors = dict(result.errors)
        for name, manifest in result.manifests.items():
            previous = self._clients.get(name)
            if previous is not None and previous.manifest == manifest:
                clients[name] = previous  # keep rate-limit and cache state for unchanged services
            else:
                clients[name] = ServiceClient(manifest, self._store, self._logins, self._downloads)

        tools: dict[str, tuple[str, str, Role]] = {}
        hidden: dict[str, str] = {}
        for client in clients.values():
            for tool in client.manifest.tools:
                if not tool.exposed:
                    hidden[f"{client.name}.{tool.name}"] = (
                        "messaging tools are off until the manifest sets enabled: true"
                        if tool.messaging and tool.enabled is not True
                        else "enabled: false"
                    )
                    continue
                if tool.is_write:
                    entries = [
                        (f"{client.name}_prepare_{tool.name}", "prepare"),
                        (f"{client.name}_confirm_{tool.name}", "confirm"),
                    ]
                else:
                    entries = [(f"{client.name}_{tool.name}", "call")]
                for exposed, role in entries:
                    if exposed in COMMON_TOOLS or exposed in tools:
                        errors[client.name] = f"tool name {exposed!r} collides with another tool"
                        continue
                    tools[exposed] = (client.name, tool.name, role)

        changed = tools != self._tools or _schemas(clients, tools) != _schemas(self._clients, self._tools)
        self._clients, self._tools, self.errors, self.hidden = clients, tools, errors, hidden
        for name, message in errors.items():
            log.warning("service %s not loaded: %s", name, message)
        return changed

    # ----- MCP surface ----------------------------------------------------

    def list_tools(self) -> list[types.Tool]:
        tools: list[types.Tool] = []
        for exposed, (service, tool_name, role) in sorted(self._tools.items()):
            client = self._clients[service]
            tool = client.tool(tool_name)
            profiles = client.manifest.profiles
            if role == "call":
                tools.append(types.Tool(
                    name=exposed,
                    description=tool.description or f"{tool.kind} tool for {service}",
                    inputSchema=_with_profile(tool.params, profiles),
                    annotations=types.ToolAnnotations(
                        readOnlyHint=tool.kind == "read", destructiveHint=False, openWorldHint=True
                    ),
                ))
            elif role == "prepare":
                confirm_name = f"{service}_confirm_{tool_name}"
                tools.append(types.Tool(
                    name=exposed,
                    description=(
                        f"Step 1 of 2. {tool.description or tool.name} "
                        f"Checks the input and limits and returns a summary and a one-time code valid for 5 minutes. "
                        f"Nothing is sent to the service. Then call {confirm_name} with the code."
                    ),
                    inputSchema=_with_profile(write_input_schema(tool), profiles),
                    annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
                ))
            else:
                prepare_name = f"{service}_prepare_{tool_name}"
                tools.append(types.Tool(
                    name=exposed,
                    description=(
                        f"Step 2 of 2. Performs {tool.name} on {service} after the user approves it "
                        f"in a confirmation dialog or page. Needs the code from {prepare_name}. "
                        f"The model cannot approve; only the user can."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {"code": {"type": "string", "description": f"code from {prepare_name}"}},
                        "required": ["code"],
                    },
                    annotations=types.ToolAnnotations(
                        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
                    ),
                ))
        tools.extend(self._common_tools())
        return tools

    def _common_tools(self) -> list[types.Tool]:
        services = sorted(self._clients)
        service_prop = {"type": "string", "enum": services} if services else {"type": "string"}
        return [
            types.Tool(
                name="list_services",
                description="List loaded services, their profiles, session status, tools and load errors.",
                inputSchema={"type": "object", "properties": {}},
                annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
            ),
            types.Tool(
                name="forget_session",
                description="Delete the local session for a service without contacting the service.",
                inputSchema={
                    "type": "object",
                    "properties": {"service": service_prop, "profile": {"type": "string"}},
                    "required": ["service"],
                },
                annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
            ),
            types.Tool(
                name="logout",
                description="Log out of a service (calls its logout endpoint if configured) and delete the local session.",
                inputSchema={
                    "type": "object",
                    "properties": {"service": service_prop},
                    "required": ["service"],
                },
                annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
            ),
        ]

    async def call(self, name: str, arguments: dict[str, Any], ctx: CallContext | None = None) -> list[types.TextContent]:
        ctx = ctx or CallContext()
        if name in COMMON_TOOLS:
            result: Any = await self._call_common(name, arguments)
        elif name in self._tools:
            service, tool_name, role = self._tools[name]
            client = self._clients[service]
            if role == "call":
                result = await client.call(tool_name, arguments)
            elif role == "prepare":
                result = self._prepare(client, tool_name, arguments)
            else:
                result = await self._confirm(client, tool_name, arguments, ctx)
        else:
            raise ServiceError(f"unknown tool {name!r}")
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        return [types.TextContent(type="text", text=text)]

    # ----- write flow -----------------------------------------------------

    def _prepare(self, client: ServiceClient, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = client.tool(tool_name)
        args = dict(arguments)
        profile = client.resolve_profile(args.pop("profile", None))
        try:
            validate_write(tool, args)
        except WriteError as exc:
            self.audit.write("refused", service=client.name, tool=tool.name, profile=profile, call=args, reason=str(exc))
            raise
        summary = build_summary(client.name, tool, args, profile)
        action = self.pending.create(client.name, tool.name, profile, args, summary)
        self.audit.write("prepared", service=client.name, tool=tool.name, profile=profile, call=args, code=action.code)
        return {
            "status": "prepared",
            "summary": summary,
            "code": action.code,
            "expires_in_seconds": self.pending.remaining_seconds(action),
            "next_step": (
                f"Call {client.name}_confirm_{tool.name} with this code. The user is asked to approve "
                f"before anything is sent. Do not approve on the user's behalf."
            ),
        }

    async def _confirm(self, client: ServiceClient, tool_name: str, arguments: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        action = self.pending.get(arguments.get("code"), client.name, tool_name)
        tool = client.tool(tool_name)
        base = {"service": client.name, "tool": tool.name, "profile": action.profile, "code": action.code}

        # The manifest may have changed since prepare: check again against the current rules.
        try:
            validate_write(tool, action.arguments)
        except WriteError as exc:
            self.pending.discard(action.code)
            self.audit.write("refused", **base, call=action.arguments, reason=str(exc))
            raise

        if action.status == "pending":
            if action.page_token is None and client_supports_elicitation(ctx.session):
                await elicit_approval(ctx.session, action, ctx.request_id)
            else:
                if action.page_token is None and not has_display():
                    raise WriteError("approval needs a graphical desktop or a client that supports elicitation")
                await self.approval.request(action)

        if action.status == "declined":
            self.pending.discard(action.code)
            self.audit.write("declined", **base, call=action.arguments)
            return {"status": "declined", "message": "The user declined. Nothing was sent."}
        if action.status == "pending":
            return {
                "status": "waiting_for_approval",
                "message": (
                    "A confirmation page is open in the browser. Ask the user to approve or cancel there, "
                    "then call this tool again with the same code."
                ),
                "expires_in_seconds": self.pending.remaining_seconds(action),
            }

        if action.executing:
            raise WriteError("this action is already being performed")
        action.executing = True
        self.pending.discard(action.code)  # one-time: the code cannot be used again
        self.audit.write("approved", **base, call=action.arguments)
        try:
            outcome = await client.execute_write(tool.name, action.profile, action.arguments)
        except Exception as exc:
            self.audit.write("failed", **base, call=action.arguments, error=str(exc))
            raise
        self.audit.write("executed", **base, call=action.arguments, response=outcome)
        if not outcome["ok"]:
            return {"status": "failed", **outcome}
        return {"status": "done", **outcome}

    # ----- common tools ---------------------------------------------------

    async def _call_common(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "list_services":
            return self.status()
        client = self._client(arguments.get("service"))
        if name == "forget_session":
            removed = client.forget_session(arguments.get("profile"))
            return {"service": client.name, "removed_profiles": removed}
        if name == "logout":
            remote = await client.logout()
            return {"service": client.name, "remote": remote, "local_session": "deleted"}
        raise ServiceError(f"unknown tool {name!r}")

    def _client(self, service: Any) -> ServiceClient:
        if not isinstance(service, str) or service not in self._clients:
            raise ServiceError(f"unknown service {service!r}; loaded: {sorted(self._clients)}")
        return self._clients[service]

    def status(self) -> dict[str, Any]:
        pending = self._logins.pending()
        services = {}
        for name, client in sorted(self._clients.items()):
            services[name] = {
                "base_url": client.manifest.base_url,
                "profiles": client.session_status(),
                "tools": sorted(t for t, (svc, _, _) in self._tools.items() if svc == name),
                "hidden_tools": {k.split(".", 1)[1]: v for k, v in self.hidden.items() if k.startswith(name + ".")},
            }
        return {
            "services": services,
            "errors": self.errors,
            "login_in_progress": {"service": pending[0], "profile": pending[1]} if pending else None,
        }


def _with_profile(params: dict[str, Any], profiles: list[str]) -> dict[str, Any]:
    schema = copy.deepcopy(params) if params else {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema["properties"]["profile"] = {"type": "string", "enum": profiles}
    return schema


def _schemas(clients: dict[str, ServiceClient], tools: dict[str, tuple[str, str, Role]]) -> dict[str, Any]:
    out = {}
    for exposed, (service, tool_name, _role) in tools.items():
        if service in clients:
            out[exposed] = clients[service].tool(tool_name).model_dump()
    return out

