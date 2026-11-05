"""`local` login provider: a visible browser window with a persistent profile per service and profile.

The user logs in by hand. The provider polls the service's session-check endpoint from
inside the browser context and captures cookies (and a bearer token, if configured)
once the check returns 200. The password is never read by this code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..config import Paths
from ..manifest import Manifest
from ..notify import notify
from ..session_store import StoredSession
from .base import LoginError

log = logging.getLogger("portal_mcp.login.local")


class LocalLoginProvider:
    name = "local"

    def __init__(self, paths: Paths, timeout_seconds: float = 600, poll_seconds: float = 2.0):
        self._paths = paths
        self._timeout = timeout_seconds
        self._poll = poll_seconds

    async def login(self, manifest: Manifest, profile: str) -> StoredSession:
        from playwright.async_api import async_playwright

        profile_dir = self._paths.profiles / manifest.name / profile
        profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(profile_dir, 0o700)
        headless = os.environ.get("PORTAL_MCP_HEADLESS") == "1"

        await notify(
            "Inloggning väntar",
            f"Logga in på {manifest.name} ({profile}) i webbläsarfönstret. Runtimen väntar på dig.",
        )
        async with async_playwright() as playwright:
            context = await _launch(playwright, profile_dir, headless)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(manifest.login_url)
                deadline = time.monotonic() + self._timeout
                while True:
                    session = await _capture(context, page, manifest)
                    if session is not None:
                        return session
                    if time.monotonic() > deadline:
                        raise LoginError(f"login to {manifest.name} timed out")
                    await asyncio.sleep(self._poll)
            finally:
                await context.close()


SANDBOX_FLAG = "--no-sandbox"


def _sandboxed(options: dict[str, Any]) -> dict[str, Any]:
    """Launch options with Chrome's sandbox always on.

    Playwright adds --no-sandbox unless chromium_sandbox is set. Logins to banks and other
    sensitive services must never run without the sandbox, so callers cannot turn it off.
    """
    options = dict(options)
    if options.pop("chromium_sandbox", True) is not True:
        raise LoginError("the browser sandbox cannot be turned off")
    options["args"] = [a for a in options.pop("args", None) or [] if a != SANDBOX_FLAG]
    options["chromium_sandbox"] = True
    return options


async def _launch(playwright: Any, profile_dir: Path, headless: bool, **options: Any) -> Any:
    """Prefer the installed Google Chrome. Fall back to Playwright's Chromium.

    Both are started with Chrome's sandbox on; there is no fallback without it.
    Extra options are passed to Playwright (the recorder uses them for HAR output).
    """
    options = _sandboxed(options)
    try:
        return await playwright.chromium.launch_persistent_context(
            str(profile_dir), channel="chrome", headless=headless, **options
        )
    except Exception as chrome_error:
        log.info("Google Chrome not usable with the sandbox on (%s); trying Playwright Chromium", chrome_error)
        first_error = chrome_error
    try:
        return await playwright.chromium.launch_persistent_context(str(profile_dir), headless=headless, **options)
    except Exception as exc:
        raise LoginError(
            "could not start a browser with Chrome's sandbox on. "
            f"Google Chrome: {first_error}. Playwright Chromium: {exc}. "
            "Install Google Chrome, or run: uv run playwright install chromium. "
            "portal-mcp never starts the browser without the sandbox."
        ) from exc


async def _capture(context: Any, page: Any, manifest: Manifest) -> StoredSession | None:
    check_url = manifest.base_url.rstrip("/") + manifest.session.check
    try:
        response = await context.request.get(check_url, max_redirects=0)
    except Exception:
        return None
    if response.status != 200:
        return None

    cookies = await context.cookies([manifest.base_url])
    session = StoredSession(
        cookies=[
            {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"]}
            for c in cookies
        ]
    )

    csrf = manifest.session.csrf
    if csrf is not None:
        for cookie in cookies:
            if cookie["name"] == csrf.from_cookie:
                session.csrf = cookie["value"]
                break

    if manifest.session.type == "bearer":
        token = await _read_storage(page, manifest.session.bearer_source or "")
        if not token:
            return None
        session.bearer = token
    return session


async def _read_storage(page: Any, source: str) -> str | None:
    """source is "localStorage:<key>" or "sessionStorage:<key>"."""
    kind, _, key = source.partition(":")
    if kind not in ("localStorage", "sessionStorage") or not key:
        raise LoginError(f"unsupported bearer_source {source!r}")
    value = await page.evaluate(f"() => window.{kind}.getItem({key!r})")
    return value or None
