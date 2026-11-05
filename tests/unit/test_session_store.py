import os
import sys
import stat

import pytest
from cryptography.fernet import Fernet

from portal_mcp.session_store import SessionStore, SessionStoreError, StoredSession

from tests.conftest import KEY


def _session() -> StoredSession:
    return StoredSession(
        cookies=[{"name": "sid", "value": "abc", "domain": "example.com", "path": "/"}],
        csrf="tok",
    )


def test_roundtrip(store):
    store.save("svc", "default", _session())
    loaded = store.load("svc", "default")
    assert loaded is not None
    assert loaded.cookies[0]["value"] == "abc"
    assert loaded.csrf == "tok"


def test_file_is_encrypted_and_private(store, paths):
    store.save("svc", "default", _session())
    path = paths.sessions / "svc__default.bin"
    assert b"abc" not in path.read_bytes()
    if sys.platform != "win32":  # POSIX file modes; Windows protects the user profile by ACLs
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_missing_session_returns_none(store):
    assert store.load("svc", "default") is None
    assert not store.has("svc", "default")


def test_profiles_are_separate(store):
    store.save("svc", "a", _session())
    assert store.has("svc", "a")
    assert not store.has("svc", "b")


def test_delete(store):
    store.save("svc", "default", _session())
    assert store.delete("svc", "default") is True
    assert store.delete("svc", "default") is False
    assert store.load("svc", "default") is None


def test_wrong_key_gives_clear_error(paths):
    writer = SessionStore(paths.sessions, key_provider=lambda: KEY)
    writer.save("svc", "default", _session())
    reader = SessionStore(paths.sessions, key_provider=lambda: Fernet.generate_key())
    with pytest.raises(SessionStoreError, match="log in again"):
        reader.load("svc", "default")
