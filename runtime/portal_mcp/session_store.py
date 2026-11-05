"""Encrypted storage for login sessions.

The Fernet key lives in the system keyring (Keychain on macOS, Credential Manager on
Windows, Secret Service on Linux). Session files are encrypted and written with mode 0600.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEYRING_SERVICE = "portal-mcp"
KEYRING_USER = "session-encryption-key"


class SessionStoreError(RuntimeError):
    pass


@dataclass
class StoredSession:
    cookies: list[dict[str, str]] = field(default_factory=list)  # name, value, domain, path
    bearer: str | None = None
    csrf: str | None = None
    saved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def keyring_key_provider() -> bytes:
    import keyring

    try:
        existing = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception as exc:  # no backend available, for example headless Linux
        raise SessionStoreError(f"system keyring is not available: {exc}") from exc
    if existing:
        return existing.encode("ascii")
    key = Fernet.generate_key()
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.decode("ascii"))
    return key


class SessionStore:
    def __init__(self, sessions_dir: Path, key_provider: Callable[[], bytes] = keyring_key_provider):
        self._dir = sessions_dir
        self._key_provider = key_provider
        self._fernet: Fernet | None = None

    def _cipher(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(self._key_provider())
        return self._fernet

    def _path(self, service: str, profile: str) -> Path:
        return self._dir / f"{service}__{profile}.bin"

    def save(self, service: str, profile: str, session: StoredSession) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        token = self._cipher().encrypt(json.dumps(asdict(session)).encode("utf-8"))
        target = self._path(service, profile)
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(token)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def load(self, service: str, profile: str) -> StoredSession | None:
        path = self._path(service, profile)
        if not path.exists():
            return None
        try:
            raw = self._cipher().decrypt(path.read_bytes())
        except InvalidToken as exc:
            raise SessionStoreError(
                "stored session cannot be decrypted (the keyring key changed?); log in again"
            ) from exc
        return StoredSession(**json.loads(raw.decode("utf-8")))

    def delete(self, service: str, profile: str) -> bool:
        path = self._path(service, profile)
        if path.exists():
            path.unlink()
            return True
        return False

    def has(self, service: str, profile: str) -> bool:
        return self._path(service, profile).exists()

    def saved_at(self, service: str, profile: str) -> str | None:
        session = self.load(service, profile)
        return session.saved_at if session else None
