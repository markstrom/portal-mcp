"""`local` login provider: a visible browser window with a persistent profile per service and profile.

The user logs in by hand. While they do, the provider polls the service's session check and
captures cookies (and a bearer token, if configured) once the check returns 200. The password
is never read by this code.

The check is sent with a copy of the browser's cookies from a short-lived HTTP client of its
own, never through the browser profile. Many sign-in systems set a new cookie (for example an
OpenID Connect nonce) on every redirect to the login; if those landed in the profile, polling
would build them up until the service rejects the request as too large.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

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


_redirect_warned: set[str] = set()


async def check_status(
    manifest: Manifest,
    cookies: list[dict[str, Any]],
    *,
    bearer: str | None = None,
    user_agent: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int | None:
    """Call the session check with the given cookies. Returns the HTTP status, or None.

    The client is created for this one request and thrown away, so cookies the service sets
    in its answer never reach the browser profile.
    """
    check_url = manifest.base_url.rstrip("/") + manifest.session.check
    headers = {"accept": "application/json, text/html;q=0.9, */*;q=0.5"}
    if cookies:
        headers["cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    if user_agent:
        headers["user-agent"] = user_agent
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=15, transport=transport) as client:
            response = await client.get(check_url, headers=headers)
    except httpx.HTTPError:
        return None
    return response.status_code


async def _user_agent(page: Any) -> str | None:
    if page is None:
        return None
    try:
        value = await page.evaluate("() => navigator.userAgent")
    except Exception:
        return None
    return value if isinstance(value, str) else None


async def _capture(
    context: Any, page: Any, manifest: Manifest, *, transport: httpx.AsyncBaseTransport | None = None
) -> StoredSession | None:
    # Only cookies that the browser would send to the service's own host.
    cookies = await context.cookies([manifest.base_url])

    bearer = None
    if manifest.session.type == "bearer":
        bearer = await _read_storage(page, manifest.session.bearer_source or "")
        if not bearer:
            return None

    status = await check_status(
        manifest, cookies, bearer=bearer, user_agent=await _user_agent(page), transport=transport
    )
    if status is not None and 300 <= status < 400 and manifest.name not in _redirect_warned:
        _redirect_warned.add(manifest.name)
        log.warning(
            "the session check for %s answers with a redirect while logged out; "
            "an endpoint that answers 401 is a better session.check",
            manifest.name,
        )
    if status != 200:
        return None

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

    if bearer:
        session.bearer = bearer
    return session


async def _read_storage(page: Any, source: str) -> str | None:
    """source is "localStorage:<key>" or "sessionStorage:<key>"."""
    kind, _, key = source.partition(":")
    if kind not in ("localStorage", "sessionStorage") or not key:
        raise LoginError(f"unsupported bearer_source {source!r}")
    value = await page.evaluate(f"() => window.{kind}.getItem({key!r})")
    return value or None
