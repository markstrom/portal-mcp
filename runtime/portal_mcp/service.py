"""One service: session handling, rate limiting, caching and HTTP calls for its tools."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .config import has_display
from .diagnostics import header_too_large_message, is_header_too_large
from .login.base import LoginError, LoginManager
from .manifest import Manifest, ToolConfig
from .session_store import SessionStore, StoredSession
from .transform import pick, redact

log = logging.getLogger("portal_mcp.service")

SESSION_CHECK_INTERVAL = 60.0  # the session is checked at most once per minute per profile
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
PATH_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class ServiceError(RuntimeError):
    pass


class NeedsLogin(ServiceError):
    """Raised when a login window has been opened and the user must finish it."""


class RateLimited(ServiceError):
    pass


class RateLimiter:
    def __init__(self, per_minute: int, clock=time.monotonic):
        self._limit = per_minute
        self._calls: deque[float] = deque()
        self._clock = clock

    def acquire(self) -> None:
        now = self._clock()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= self._limit:
            raise RateLimited(f"rate limit of {self._limit} calls per minute reached; try again shortly")
        self._calls.append(now)


class ServiceClient:
    def __init__(
        self,
        manifest: Manifest,
        store: SessionStore,
        logins: LoginManager,
        downloads: Path,
        clock=time.monotonic,
    ):
        self.manifest = manifest
        self._store = store
        self._logins = logins
        self._downloads = downloads
        self._clock = clock
        self._rate = RateLimiter(manifest.rate_limit_per_minute, clock)
        self._cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
        self._checked: dict[str, float] = {}
        self.login_wait_seconds = 20.0  # how long a tool call waits for a login before returning

    @property
    def name(self) -> str:
        return self.manifest.name

    def tool(self, tool_name: str) -> ToolConfig:
        for tool in self.manifest.tools:
            if tool.name == tool_name:
                return tool
        raise ServiceError(f"unknown tool {tool_name!r}")

    def resolve_profile(self, profile: str | None) -> str:
        if profile is None:
            return self.manifest.profiles[0]
        if profile not in self.manifest.profiles:
            raise ServiceError(f"unknown profile {profile!r} for {self.name}; known: {self.manifest.profiles}")
        return profile

    # ----- sessions -------------------------------------------------------

    async def _ensure_session(self, profile: str, *, force_login: bool = False) -> StoredSession | None:
        if not self.manifest.needs_login:
            return None  # a public site: no session, never a login window
        session = None if force_login else self._store.load(self.name, profile)
        if session is not None:
            last = self._checked.get(profile)
            if last is not None and self._clock() - last < SESSION_CHECK_INTERVAL:
                return session
            status = await self._check(session)
            if status == 200:
                self._checked[profile] = self._clock()
                return session
            if status not in (401, 403):
                raise ServiceError(f"session check for {self.name} returned HTTP {status}")
            log.info("session for %s/%s is no longer valid", self.name, profile)

        return await self._login(profile)

    async def _login(self, profile: str) -> StoredSession:
        if not has_display():
            raise ServiceError(
                "login needs a graphical desktop. No DISPLAY or WAYLAND_DISPLAY was found."
            )
        try:
            session = await self._logins.ensure(self.manifest, profile, wait_seconds=self.login_wait_seconds)
        except LoginError as exc:
            raise ServiceError(str(exc)) from exc
        if session is None:
            raise NeedsLogin(
                f"a login window for {self.name} is open. Log in there, then call this tool again."
            )
        self._checked[profile] = self._clock()
        self._cache = {k: v for k, v in self._cache.items() if k[0] != profile}
        return session

    async def _check(self, session: StoredSession) -> int:
        async with self._http(session) as client:
            try:
                response = await client.get(self.manifest.session.check)
            except httpx.HTTPError as exc:
                raise ServiceError(f"could not reach {self.name}: {exc}") from exc
            return response.status_code

    def _http(self, session: StoredSession | None) -> httpx.AsyncClient:
        cookies = httpx.Cookies()
        if session is not None:
            for cookie in session.cookies:
                cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path", "/"))
        headers = {"accept": "application/json, */*;q=0.5"}
        if session is not None:
            if session.bearer:
                headers["authorization"] = f"Bearer {session.bearer}"
            csrf = self.manifest.session.csrf
            if csrf is not None and session.csrf:
                headers[csrf.header] = session.csrf
        return httpx.AsyncClient(
            base_url=self.manifest.base_url,
            cookies=cookies,
            headers=headers,
            timeout=30,
            follow_redirects=False,
        )

    async def logout(self, profile: str | None = None) -> str:
        """Call the service's logout endpoint, if any, and delete the local session.

        With a profile, only that profile is logged out; otherwise all of them.
        """
        if not self.manifest.needs_login:
            return "this service has no login (session.type none); nothing to log out of"
        remote = "no logout endpoint configured"
        profiles = [self.resolve_profile(profile)] if profile else self.manifest.profiles
        for profile in profiles:
            session = self._store.load(self.name, profile)
            if session is None or not self.manifest.session.logout:
                continue
            try:
                async with self._http(session) as client:
                    response = await client.post(self.manifest.session.logout)
                remote = f"logout endpoint answered HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                remote = f"logout endpoint could not be reached: {exc}"
        for name in profiles:
            self.forget_session(name)
        return remote

    def forget_session(self, profile: str | None = None) -> list[str]:
        """Delete local sessions. Returns the profiles that had one."""
        removed = []
        for name in [profile] if profile else self.manifest.profiles:
            if self._store.delete(self.name, name):
                removed.append(name)
            self._checked.pop(name, None)
        self._cache.clear()
        return removed

    def session_status(self) -> dict[str, str | None]:
        if not self.manifest.needs_login:
            return {profile: "no login needed" for profile in self.manifest.profiles}
        return {profile: self._store.saved_at(self.name, profile) for profile in self.manifest.profiles}

    # ----- tool calls -----------------------------------------------------

    async def call(self, tool_name: str, arguments: dict[str, Any], *, use_cache: bool = True) -> Any:
        tool = self.tool(tool_name)
        if tool.is_write:
            raise ServiceError(f"{tool.name} is a write tool; use prepare and confirm")
        arguments = dict(arguments)
        profile = self.resolve_profile(arguments.pop("profile", None))

        cache_key = (profile, tool.name, json.dumps(arguments, sort_keys=True, default=str))
        if use_cache and tool.kind == "read" and self.manifest.cache_seconds > 0:
            hit = self._cache.get(cache_key)
            if hit is not None and self._clock() < hit[0]:
                return hit[1]

        self._rate.acquire()
        session = await self._ensure_session(profile)
        response = await self._send(tool, arguments, session)
        if response.status_code in (401, 403) and not self.manifest.needs_login:
            raise ServiceError(
                f"{self.name} answered HTTP {response.status_code} for {tool.name}. This service is set up "
                "without login (session.type none), so the page may need a login after all."
            )
        if response.status_code in (401, 403):
            log.info("%s rejected the session; logging in again", self.name)
            session = await self._ensure_session(profile, force_login=True)
            response = await self._send(tool, arguments, session)
            if response.status_code in (401, 403):
                raise ServiceError(f"{self.name} rejected the session after logging in again")

        if response.status_code >= 400:
            raise ServiceError(f"{self.name} answered HTTP {response.status_code} for {tool.name}")

        if tool.kind == "file":
            return self._save_file(tool, response)

        result = self._shape_json(tool, response)
        if tool.kind == "read" and self.manifest.cache_seconds > 0:
            self._cache[cache_key] = (self._clock() + self.manifest.cache_seconds, result)
        return result

    async def execute_write(self, tool_name: str, profile: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Send an approved write. Only called by the confirm step after human approval."""
        tool = self.tool(tool_name)
        if not tool.is_write:
            raise ServiceError(f"{tool.name} is not a write tool")
        self._rate.acquire()
        session = await self._ensure_session(profile)
        response = await self._send(tool, arguments, session)
        if response.status_code == 401 and self.manifest.needs_login:
            # 401 means the request was not accepted, so one retry after a new login is safe.
            log.info("%s rejected the session; logging in again before retrying the write", self.name)
            session = await self._ensure_session(profile, force_login=True)
            response = await self._send(tool, arguments, session)
        self._cache.clear()  # data may have changed
        body: Any
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                body = response.json()
                if self.manifest.redact:
                    body = redact(body, self.manifest.redact)
            except ValueError:
                body = response.text[:4000]
        else:
            body = response.text[:4000]
        return {"http_status": response.status_code, "ok": response.status_code < 400, "response": body}

    async def _send(self, tool: ToolConfig, arguments: dict[str, Any], session: StoredSession | None) -> httpx.Response:
        args = dict(arguments)
        path = tool.path
        for match in PATH_PARAM.findall(tool.path):
            if match not in args:
                raise ServiceError(f"missing argument {match!r} for {tool.name}")
            path = path.replace("{" + match + "}", quote(str(args.pop(match)), safe=""))

        async with self._http(session) as client:
            try:
                if tool.method == "GET":
                    response = await client.request("GET", path, params=_query(args))
                else:
                    body = {**tool.body_constants, **args}
                    if not body:
                        response = await client.request(tool.method, path)
                    elif tool.body_encoding == "form":
                        response = await client.request(tool.method, path, data=_form(body))
                    else:
                        response = await client.request(tool.method, path, json=body)
            except httpx.HTTPError as exc:
                raise ServiceError(f"could not reach {self.name}: {exc}") from exc

        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ServiceError(f"response from {self.name} is larger than {MAX_RESPONSE_BYTES} bytes")
        if is_header_too_large(response.status_code, response.text[:4000], response.reason_phrase):
            host = urlsplit(self.manifest.base_url).hostname or self.manifest.base_url
            if session is None:
                raise ServiceError(f"{self.name} rejected the request because its headers were too large")
            raise ServiceError(
                header_too_large_message(self.name, host, session.cookies)
                + " Call forget_session with browser_profile set to true, then call the tool again "
                "to log in with a clean profile."
            )
        return response

    def _shape_json(self, tool: ToolConfig, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise ServiceError(f"{self.name} did not return JSON for {tool.name} (got {content_type or 'no type'})")
        data = response.json()
        if tool.pick:
            data = pick(data, tool.pick)
        if self.manifest.redact:
            data = redact(data, self.manifest.redact)
        return data

    def _save_file(self, tool: ToolConfig, response: httpx.Response) -> dict[str, Any]:
        name = _filename_from(response.headers.get("content-disposition"))
        if not name:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = f"{self.name}-{tool.name}-{stamp}.bin"
        target = _unique(self._downloads / name)
        target.write_bytes(response.content)
        return {
            "saved_to": str(target),
            "bytes": len(response.content),
            "content_type": response.headers.get("content-type", ""),
        }


def _query(args: dict[str, Any]) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    for key, value in args.items():
        if value is None:
            continue
        if isinstance(value, list):
            params.extend((key, _scalar(v)) for v in value)
        else:
            params.append((key, _scalar(value)))
    return params


def _form(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in body.items():
        if isinstance(value, list):
            out[key] = [_scalar(v) for v in value]
        elif isinstance(value, dict):
            out[key] = json.dumps(value, ensure_ascii=False)
        elif value is not None:
            out[key] = _scalar(value)
    return out


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _filename_from(header: str | None) -> str | None:
    if not header:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', header)
    if not match:
        return None
    name = Path(match.group(1).strip()).name  # strip any directory parts from the server
    name = re.sub(r"[^\w.\- ()]", "_", name).strip(" .")
    return name or None


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
