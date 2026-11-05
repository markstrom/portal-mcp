"""Desktop notifications, one method per system. Failures are logged and never raised.

- macOS: `osascript` (desktop-notifier needs a signed app bundle on macOS and fails silently
  when the runtime runs from a plain Python interpreter).
- Linux: `notify-send` when available, otherwise desktop-notifier (D-Bus).
- Windows: desktop-notifier (WinRT).

Set PORTAL_MCP_NO_NOTIFY=1 to turn notifications off.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys

log = logging.getLogger("portal_mcp.notify")

TIMEOUT_SECONDS = 5

# AppleScript reads title and message from argv, so no text is ever spliced into the script.
_APPLESCRIPT = [
    "-e", "on run argv",
    "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
    "-e", "end run",
]


async def _run(*command: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_SECONDS)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("notification command failed: %s", exc)
        return False
    if process.returncode != 0:
        log.warning("notification command exited with %s: %s", process.returncode, stderr.decode(errors="replace").strip())
        return False
    return True


async def _desktop_notifier(title: str, message: str) -> bool:
    try:
        from desktop_notifier import DesktopNotifier

        await asyncio.wait_for(
            DesktopNotifier(app_name="portal-mcp").send(title=title, message=message), timeout=TIMEOUT_SECONDS
        )
        return True
    except Exception as exc:
        log.warning("could not send desktop notification: %s", exc)
        return False


async def notify(title: str, message: str) -> bool:
    """Show a desktop notification. Returns True if a method reported success."""
    if os.environ.get("PORTAL_MCP_NO_NOTIFY") == "1":
        log.info("notification (suppressed): %s - %s", title, message)
        return False
    title, message = title[:120], message[:400]
    if sys.platform == "darwin" and shutil.which("osascript"):
        return await _run("osascript", *_APPLESCRIPT, title, message)
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        return await _run("notify-send", "--app-name=portal-mcp", title, message)
    return await _desktop_notifier(title, message)
