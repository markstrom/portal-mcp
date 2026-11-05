"""Builds the tool list from manifests and routes tool calls to the right service.

Each service is isolated: a broken manifest is recorded in `errors` and the rest keep working.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from mcp import types

from .config import Paths
from .login.base import LoginManager
from .manifest import Manifest, load_manifests
from .service import ServiceClient, ServiceError
from .session_store import SessionStore

log = logging.getLogger("portal_mcp.registry")

EXPOSED_KINDS = {"read", "file"}  # write and binding tools are added in phase 3
COMMON_TOOLS = {"list_services", "forget_session", "logout"}


class Registry:
    def __init__(self, paths: Paths, store: SessionStore, logins: LoginManager, downloads):
        self._paths = paths
        self._store = store
        self._logins = logins
        self._downloads = downloads
        self._clients: dict[str, ServiceClient] = {}
        self._tools: dict[str, tuple[str, str]] = {}  # exposed tool name -> (service, tool)
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

        tools: dict[str, tuple[str, str]] = {}
        for client in clients.values():
            for tool in client.manifest.tools:
                exposed = f"{client.name}_{tool.name}"
                if tool.kind not in EXPOSED_KINDS:
                    continue
                if exposed in COMMON_TOOLS or exposed in tools:
                    errors[client.name] = f"tool name {exposed!r} collides with another tool"
                    continue
                tools[exposed] = (client.name, tool.name)

        changed = tools.keys() != self._tools.keys() or _schemas(clients, tools) != _schemas(self._clients, self._tools)
        self._clients, self._tools, self.errors = clients, tools, errors
        for name, message in errors.items():
            log.warning("service %s not loaded: %s", name, message)
        return changed

    # ----- MCP surface ----------------------------------------------------

    def list_tools(self) -> list[types.Tool]:
        tools: list[types.Tool] = []
        for exposed, (service, tool_name) in sorted(self._tools.items()):
            client = self._clients[service]
            tool = client.tool(tool_name)
            tools.append(
                types.Tool(
                    name=exposed,
                    description=tool.description or f"{tool.kind} tool for {service}",
                    inputSchema=_input_schema(tool.params, client.manifest.profiles),
                    annotations=types.ToolAnnotations(
                        readOnlyHint=tool.kind == "read",
                        destructiveHint=False,
                        openWorldHint=True,
                    ),
                )
            )
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

    async def call(self, name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        if name in COMMON_TOOLS:
            result: Any = await self._call_common(name, arguments)
        elif name in self._tools:
            service, tool_name = self._tools[name]
            result = await self._clients[service].call(tool_name, arguments)
        else:
            raise ServiceError(f"unknown tool {name!r}")
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        return [types.TextContent(type="text", text=text)]

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
                "tools": sorted(t for t, (svc, _) in self._tools.items() if svc == name),
            }
        return {
            "services": services,
            "errors": self.errors,
            "login_in_progress": {"service": pending[0], "profile": pending[1]} if pending else None,
        }


def _input_schema(params: dict[str, Any], profiles: list[str]) -> dict[str, Any]:
    schema = copy.deepcopy(params) if params else {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema["properties"]["profile"] = {"type": "string", "enum": profiles}
    return schema


def _schemas(clients: dict[str, ServiceClient], tools: dict[str, tuple[str, str]]) -> dict[str, Any]:
    out = {}
    for exposed, (service, tool_name) in tools.items():
        if service in clients:
            out[exposed] = clients[service].tool(tool_name).model_dump()
    return out
