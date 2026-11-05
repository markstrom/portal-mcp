"""Entry point: `portal-mcp-runtime` or `python -m portal_mcp`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import default_paths, downloads_dir, has_display
from .login.base import LoginManager
from .login.local import LocalLoginProvider
from .registry import Registry
from .server import build_server, run_http, run_stdio
from .session_store import SessionStore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="portal-mcp-runtime", description="portal-mcp MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP on localhost instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # stdout belongs to the MCP protocol
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("portal_mcp")

    paths = default_paths()
    paths.ensure()
    store = SessionStore(paths.sessions)
    logins = LoginManager(LocalLoginProvider(paths), store)
    registry = Registry(paths, store, logins, downloads_dir())
    if not has_display():
        log.warning("no DISPLAY or WAYLAND_DISPLAY: login needs a graphical desktop and will fail")
    for name, message in registry.errors.items():
        log.warning("service %s not loaded: %s", name, message)

    server, state = build_server(registry)
    if args.http:
        asyncio.run(run_http(server, registry, state, args.host, args.port))
    else:
        asyncio.run(run_stdio(server, registry, state))


if __name__ == "__main__":
    main()
