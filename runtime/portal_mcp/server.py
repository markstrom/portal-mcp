"""MCP server: stdio by default, streamable HTTP on 127.0.0.1 as an opt-in.

Logging goes to stderr only. stdout is reserved for the MCP protocol.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http import StreamableHTTPServerTransport

from . import __version__
from .registry import Registry

log = logging.getLogger("portal_mcp.server")


def build_server(registry: Registry) -> tuple[Server, dict[str, Any]]:
    server: Server = Server("portal-mcp", version=__version__)
    state: dict[str, Any] = {"session": None}

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        try:
            state["session"] = server.request_context.session
        except LookupError:
            pass
        return registry.list_tools()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
        return await registry.call(name, arguments or {})

    return server, state


def _init_options(server: Server):
    return server.create_initialization_options(notification_options=NotificationOptions(tools_changed=True))


async def _watch_manifests(registry: Registry, state: dict[str, Any]) -> None:
    """Reload manifests when files change and tell the client that the tool list changed."""
    from watchfiles import awatch

    registry.services_dir.mkdir(parents=True, exist_ok=True)
    try:
        async for _changes in awatch(registry.services_dir):
            if not registry.reload():
                continue
            session = state.get("session")
            if session is None:
                log.info("manifests changed; the client has not listed tools yet")
                continue
            try:
                await session.send_tool_list_changed()
            except Exception as exc:
                log.warning("could not send tools/list_changed: %s", exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("manifest watcher stopped: %s", exc)


async def run_stdio(server: Server, registry: Registry, state: dict[str, Any]) -> None:
    watcher = asyncio.create_task(_watch_manifests(registry, state))
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, _init_options(server))
    finally:
        watcher.cancel()


def build_http_app(server: Server):
    """A stateless streamable-HTTP endpoint at /mcp. Each request gets its own transport."""
    from starlette.applications import Starlette
    from starlette.routing import Mount

    async def handle_mcp(scope, receive, send) -> None:
        transport = StreamableHTTPServerTransport(mcp_session_id=None, is_json_response_enabled=True)
        async with transport.connect() as (read_stream, write_stream):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: server.run(read_stream, write_stream, _init_options(server), stateless=True)
                )
                await transport.handle_request(scope, receive, send)
                tg.cancel_scope.cancel()

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)])


async def run_http(server: Server, registry: Registry, state: dict[str, Any], host: str, port: int) -> None:
    import uvicorn

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to bind outside localhost; use 127.0.0.1")
    watcher = asyncio.create_task(_watch_manifests(registry, state))
    config = uvicorn.Config(build_http_app(server), host=host, port=port, log_level="warning")
    try:
        await uvicorn.Server(config).serve()
    finally:
        watcher.cancel()
