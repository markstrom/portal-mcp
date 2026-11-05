"""The login layer. Version 1 implements only the `local` provider.

A provider opens a login flow for one service and profile and returns the session
it captured. The provider never sees or stores the user's password: the user types
it into a visible browser window (or uses BankID, SSO or a one-time code there).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from ..manifest import Manifest
from ..notify import notify
from ..session_store import SessionStore, StoredSession

log = logging.getLogger("portal_mcp.login")


class LoginError(RuntimeError):
    pass


class LoginProvider(Protocol):
    name: str

    async def login(self, manifest: Manifest, profile: str) -> StoredSession:
        """Wait until the user is logged in and return the captured session."""
        ...


class LoginManager:
    """Runs at most one login at a time, in a task that survives client timeouts."""

    def __init__(self, provider: LoginProvider, store: SessionStore):
        self._provider = provider
        self._store = store
        self._task: asyncio.Task[StoredSession] | None = None
        self._key: tuple[str, str] | None = None

    def pending(self) -> tuple[str, str] | None:
        if self._task is not None and not self._task.done():
            return self._key
        return None

    def _start(self, manifest: Manifest, profile: str) -> asyncio.Task[StoredSession]:
        if self._task is not None and not self._task.done():
            if self._key != (manifest.name, profile):
                raise LoginError(
                    f"another login is in progress for {self._key[0]}/{self._key[1]}; finish it first"
                )
            return self._task

        async def run() -> StoredSession:
            session = await self._provider.login(manifest, profile)
            self._store.save(manifest.name, profile, session)
            log.info("login completed for %s/%s", manifest.name, profile)
            await notify("Inloggad", f"{manifest.name} ({profile}) är inloggad. Du kan fråga igen.")
            return session

        self._key = (manifest.name, profile)
        self._task = asyncio.create_task(run(), name=f"login:{manifest.name}:{profile}")
        return self._task

    async def ensure(self, manifest: Manifest, profile: str, wait_seconds: float = 20) -> StoredSession | None:
        """Wait up to wait_seconds for a login. Returns None if it is still pending.

        asyncio.shield keeps the login running when this wait times out.
        """
        if not manifest.needs_login:
            raise LoginError(f"{manifest.name} has no login (session.type none)")
        task = self._start(manifest, profile)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
        except asyncio.TimeoutError:
            return None
