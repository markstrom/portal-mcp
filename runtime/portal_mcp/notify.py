"""Cross-platform desktop notifications. Failures are logged and never raised."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("portal_mcp.notify")


async def notify(title: str, message: str) -> None:
    if os.environ.get("PORTAL_MCP_NO_NOTIFY") == "1":
        log.info("notification (suppressed): %s - %s", title, message)
        return
    try:
        from desktop_notifier import DesktopNotifier

        await DesktopNotifier(app_name="portal-mcp").send(title=title, message=message)
    except Exception as exc:  # a missing notification daemon must not break a login
        log.warning("could not send desktop notification: %s", exc)
