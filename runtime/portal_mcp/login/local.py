"""`local` login provider: a visible browser window with a persistent profile per service and profile.

The user logs in by hand. While they do, the provider polls the service's session check and
captures cookies (and a bearer token, if configured) once the check returns 200. The password
is never read by this code.

The check is sent with a copy of the browser's cookies from a short-lived HTTP client of its
own, never through the browser profile. Many sign-in systems set a new cookie (for example an
OpenID Connect nonce) on every redirect to the login; if those landed in the profile, polling
would build them up until the service rejects the request as too large.

Only a 200 from the check counts as logged in. A redirect never does. A check that answers 200
without any cookies cannot tell logged in from logged out, so the login refuses to start.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import Paths
from ..diagnostics import header_too_large_message, is_header_too_large
from ..manifest import Manifest
from ..notify import notify
from ..session_store import StoredSession
from .base import LoginError

log = logging.getLogger("portal_mcp.login.local")

START_TIMEOUT_SECONDS = 45.0  # the browser must be up within this time, or we fail with a cause
Status = Callable[[str], None]


def _log_status(message: str) -> None:
    log.info(message)


class LocalLoginProvider:
    name = "local"

    def __init__(self, paths: Paths, timeout_seconds: float = 600, poll_seconds: float = 2.0):
        self._paths = paths
        self._timeout = timeout_seconds
        self._poll = poll_seconds

    async def login(self, manifest: Manifest, profile: str) -> StoredSession:
        from playwright.async_api import async_playwright

        preflight = await preflight_check(manifest)
        if preflight.verdict == "open":
            raise LoginError(preflight.explain(manifest))
        if preflight.verdict == "redirects":
            log.warning(preflight.explain(manifest))

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
                page = await _first_page(context)
                await page.goto(manifest.login_url)
                session = await wait_for_login(context, page, manifest, self._timeout, self._poll)
                return session
            finally:
                await context.close()


# ----- browser start --------------------------------------------------------

SANDBOX_FLAG = "--no-sandbox"
_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_profile_lock(profile_dir: Path, status: Status = _log_status) -> None:
    """Chrome leaves a SingletonLock ("<host>-<pid>") in a profile it is using.

    A lock held by a running browser makes a second start hang or exit without a window, so
    it is a clear error. A lock left by a browser that crashed is removed.
    """
    lock = profile_dir / "SingletonLock"
    if not lock.is_symlink() and not lock.exists():
        return
    try:
        target = os.readlink(lock)
    except OSError:
        target = ""
    host, _, pid_text = target.rpartition("-")
    pid = int(pid_text) if pid_text.isdigit() else None
    if pid is not None and host == socket.gethostname() and _pid_alive(pid):
        raise LoginError(
            f"the browser profile {profile_dir} is in use by another browser process (pid {pid}). "
            "Close that browser window, or start with a clean profile."
        )
    for name in _LOCK_FILES:
        path = profile_dir / name
        if path.is_symlink() or path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    status(f"Removed a stale browser lock from {profile_dir.name} (left by a browser that is no longer running).")


async def _launch(
    playwright: Any,
    profile_dir: Path,
    headless: bool,
    *,
    status: Status = _log_status,
    start_timeout: float = START_TIMEOUT_SECONDS,
    **options: Any,
) -> Any:
    """Prefer the installed Google Chrome. Fall back to Playwright's Chromium.

    Both are started with Chrome's sandbox on; there is no fallback without it. A start that
    takes longer than start_timeout fails with the likely causes instead of waiting silently.
    Extra options are passed to Playwright (the recorder uses them for HAR output).
    """
    options = _sandboxed(options)
    profile_dir.mkdir(parents=True, exist_ok=True)
    check_profile_lock(profile_dir, status)

    async def start(**channel: Any) -> Any:
        return await asyncio.wait_for(
            playwright.chromium.launch_persistent_context(str(profile_dir), headless=headless, **channel, **options),
            timeout=start_timeout,
        )

    status("Starting Google Chrome…")
    try:
        context = await start(channel="chrome")
        status("Google Chrome is running.")
        return context
    except asyncio.TimeoutError as exc:
        raise LoginError(_start_timeout_message(profile_dir, start_timeout)) from exc
    except Exception as chrome_error:
        log.info("Google Chrome not usable with the sandbox on (%s); trying Playwright Chromium", chrome_error)
        first_error = chrome_error
    status("Google Chrome did not start; starting Playwright Chromium…")
    try:
        context = await start()
        status("Playwright Chromium is running.")
        return context
    except asyncio.TimeoutError as exc:
        raise LoginError(_start_timeout_message(profile_dir, start_timeout)) from exc
    except Exception as exc:
        raise LoginError(
            "could not start a browser with Chrome's sandbox on. "
            f"Google Chrome: {first_error}. Playwright Chromium: {exc}. "
            "Install Google Chrome, or run: uv run playwright install chromium. "
            "portal-mcp never starts the browser without the sandbox."
        ) from exc


def _start_timeout_message(profile_dir: Path, timeout: float) -> str:
    return (
        f"the browser did not start within {int(timeout)} seconds. Likely causes: the profile "
        f"{profile_dir} is damaged or still in use, or the browser is waiting on a dialog that "
        "is not visible. Start again with a clean profile."
    )


async def _first_page(context: Any, timeout: float = 15.0) -> Any:
    """The window's first tab. Opening one that never appears is an error, not a silent wait."""
    if context.pages:
        return context.pages[0]
    try:
        return await asyncio.wait_for(context.new_page(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise LoginError(f"the browser started but no window opened within {int(timeout)} seconds") from exc


# ----- session check --------------------------------------------------------

@dataclass
class CheckResult:
    status: int | None
    location: str | None = None
    header_too_large: bool = False
    error: str | None = None
    content_type: str | None = None

    @property
    def location_host(self) -> str | None:
        return urlsplit(self.location).hostname if self.location else None

    @property
    def verdict(self) -> str:
        """For a check made without cookies: rejects, redirects, open or error."""
        if self.status in (401, 403):
            return "rejects"
        if self.status is not None and 300 <= self.status < 400:
            return "redirects"
        if self.status == 200:
            return "open"
        return "error"

    def explain(self, manifest: Manifest) -> str:
        check = manifest.session.check
        if self.verdict == "rejects":
            return f"session.check {check} answers {self.status} when logged out: good."
        if self.verdict == "redirects":
            where = f" to {self.location_host}" if self.location_host else ""
            return (
                f"session.check {check} redirects{where} when logged out. The login counts as verified "
                "only when it answers 200; a redirect never counts. An endpoint that answers 401 when "
                "logged out is more reliable."
            )
        if self.verdict == "open":
            return (
                f"session.check {check} answers 200 even without a login, so it cannot tell a "
                "logged-out user from a logged-in one. Choose a check that answers 401 or redirects "
                "when logged out."
            )
        return f"session.check {check} could not be reached ({self.error or self.status})."


async def check_status(
    manifest: Manifest,
    cookies: list[dict[str, Any]],
    *,
    bearer: str | None = None,
    user_agent: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CheckResult:
    """Call the session check with the given cookies.

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
    except httpx.HTTPError as exc:
        return CheckResult(status=None, error=type(exc).__name__)
    too_large = is_header_too_large(response.status_code, response.text[:4000], response.reason_phrase)
    return CheckResult(
        status=response.status_code,
        location=response.headers.get("location"),
        header_too_large=too_large,
        content_type=response.headers.get("content-type"),
    )


def manifest_with_check(manifest: Manifest, check: str) -> Manifest:
    """A copy of the manifest with another session.check (for trying a candidate)."""
    session = manifest.session.model_copy(update={"check": check})
    return manifest.model_copy(update={"session": session})


def candidate_verdict(logged_out: CheckResult, logged_in: CheckResult | None) -> tuple[bool, str]:
    """Is this endpoint a reliable session.check? (usable, reason)

    Usable only if it answers 401 or 403 without a redirect when logged out, and 200 when
    logged in. Everything else is not usable, and the reason says why.
    """
    if logged_out.status is not None and 300 <= logged_out.status < 400:
        where = f" to {logged_out.location_host}" if logged_out.location_host else ""
        return False, f"redirects{where} when logged out"
    if logged_out.status == 200:
        return False, "answers 200 even when logged out"
    if logged_out.status not in (401, 403):
        return False, f"answers {logged_out.status or logged_out.error} when logged out, not 401 or 403"
    if logged_in is None:
        return False, "not tried logged in (the login was not verified)"
    if logged_in.status != 200:
        return False, f"answers {logged_in.status or logged_in.error} when logged in, not 200"
    return True, f"answers {logged_out.status} when logged out and 200 when logged in"


async def preflight_check(manifest: Manifest, transport: httpx.AsyncBaseTransport | None = None) -> CheckResult:
    """The check without any cookies, before the browser opens."""
    return await check_status(manifest, [], transport=transport)


async def _user_agent(page: Any) -> str | None:
    if page is None:
        return None
    try:
        value = await page.evaluate("() => navigator.userAgent")
    except Exception:
        return None
    return value if isinstance(value, str) else None


@dataclass
class LoginObservation:
    """What the session check answered while waiting for the login (for error messages)."""

    last: CheckResult | None = None
    statuses: dict[str, int] = field(default_factory=dict)

    def record(self, result: CheckResult) -> None:
        self.last = result
        key = str(result.status) if result.status is not None else (result.error or "error")
        self.statuses[key] = self.statuses.get(key, 0) + 1


async def _capture(
    context: Any,
    page: Any,
    manifest: Manifest,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    observation: LoginObservation | None = None,
) -> StoredSession | None:
    # Only cookies that the browser would send to the service's own host.
    cookies = await context.cookies([manifest.base_url])

    bearer = None
    if manifest.session.type == "bearer":
        bearer = await _read_storage(page, manifest.session.bearer_source or "")
        if not bearer:
            return None

    result = await check_status(
        manifest, cookies, bearer=bearer, user_agent=await _user_agent(page), transport=transport
    )
    if observation is not None:
        observation.record(result)
    if result.header_too_large:
        host = urlsplit(manifest.base_url).hostname or manifest.base_url
        raise LoginError(header_too_large_message(manifest.name, host, cookies))
    if result.status != 200:
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


async def wait_for_login(
    context: Any,
    page: Any,
    manifest: Manifest,
    timeout: float,
    poll: float,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> StoredSession:
    """Poll until the check answers 200. On timeout, say what the check answered instead."""
    observation = LoginObservation()
    deadline = time.monotonic() + timeout
    while True:
        session = await _capture(context, page, manifest, transport=transport, observation=observation)
        if session is not None:
            return session
        if time.monotonic() > deadline:
            raise LoginError(login_not_verified_message(manifest, observation, timeout))
        await asyncio.sleep(poll)


def login_not_verified_message(manifest: Manifest, observation: LoginObservation, timeout: float) -> str:
    last = observation.last
    answered = ", ".join(f"{status} x{count}" for status, count in observation.statuses.items()) or "nothing"
    detail = ""
    if last is not None and last.status is not None and 300 <= last.status < 400:
        where = f" to {last.location_host}" if last.location_host else ""
        detail = (
            f" It kept redirecting{where}, so the service did not see a logged-in session. "
            "If you did log in, the check may be a page that always redirects; choose an endpoint "
            "that answers 200 when logged in and 401 when not."
        )
    return (
        f"the login to {manifest.name} was not verified within {int(timeout)} seconds. "
        f"session.check {manifest.session.check} answered: {answered}.{detail}"
    )


async def _read_storage(page: Any, source: str) -> str | None:
    """source is "localStorage:<key>" or "sessionStorage:<key>"."""
    kind, _, key = source.partition(":")
    if kind not in ("localStorage", "sessionStorage") or not key:
        raise LoginError(f"unsupported bearer_source {source!r}")
    value = await page.evaluate(f"() => window.{kind}.getItem({key!r})")
    return value or None
