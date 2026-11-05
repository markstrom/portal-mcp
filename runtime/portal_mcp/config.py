"""Locations for user data, kept outside the plugin so updates never touch them.

Set PORTAL_MCP_HOME to override the base directory (used by the tests).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "portal-mcp"


@dataclass(frozen=True)
class Paths:
    base: Path

    @property
    def services(self) -> Path:
        return self.base / "services"

    @property
    def profiles(self) -> Path:
        return self.base / "profiles"

    @property
    def sessions(self) -> Path:
        return self.base / "sessions"

    @property
    def recordings(self) -> Path:
        return self.base / "recordings"

    @property
    def uploads(self) -> Path:
        """Files that upload tools may send. Tools refuse paths outside this directory."""
        return self.base / "uploads"

    @property
    def audit_log(self) -> Path:
        return self.base / "audit.log"

    def ensure(self) -> None:
        """Create the data directories. Directories that hold private data get mode 0700."""
        for directory in (self.base, self.services, self.profiles, self.sessions, self.recordings, self.uploads):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)


def default_paths() -> Paths:
    override = os.environ.get("PORTAL_MCP_HOME")
    base = Path(override) if override else Path(platformdirs.user_data_dir(APP_NAME))
    return Paths(base=base)


def downloads_dir() -> Path:
    override = os.environ.get("PORTAL_MCP_DOWNLOADS")
    if override:
        path = Path(override)
    else:
        path = Path(platformdirs.user_downloads_dir())
    path.mkdir(parents=True, exist_ok=True)
    return path


def has_display() -> bool:
    """A visible browser window needs a desktop. On Linux, check for a display server."""
    import sys

    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True
