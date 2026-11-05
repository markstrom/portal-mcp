"""Manifest schema and loading. One YAML manifest per service in the services directory.

A broken manifest is reported as an error for that service only; other services still load.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

ToolKind = Literal["read", "write", "binding", "file"]
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class CsrfConfig(BaseModel):
    header: str
    from_cookie: str


class SessionConfig(BaseModel):
    type: Literal["cookies", "bearer"] = "cookies"
    bearer_source: str | None = None  # e.g. "localStorage:auth_token"
    csrf: CsrfConfig | None = None
    check: str  # path; 200 means the session is valid, 401 means it is not
    logout: str | None = None  # path, called with POST

    @model_validator(mode="after")
    def _bearer_needs_source(self) -> SessionConfig:
        if self.type == "bearer" and not self.bearer_source:
            raise ValueError("session.bearer_source is required when session.type is 'bearer'")
        if not self.check.startswith("/"):
            raise ValueError("session.check must start with '/'")
        if self.logout is not None and not self.logout.startswith("/"):
            raise ValueError("session.logout must start with '/'")
        return self


class ToolConfig(BaseModel):
    name: str
    kind: ToolKind
    description: str = ""
    method: HttpMethod = "GET"
    path: str
    params: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    pick: list[str] | None = None  # keep only these dotted field paths in JSON responses
    body_schema: dict[str, Any] | None = None
    limits: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not TOOL_RE.match(value):
            raise ValueError(f"invalid tool name {value!r}")
        return value

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("tool path must start with '/'")
        return value


class Manifest(BaseModel):
    name: str
    enabled: bool = True
    base_url: str
    login_url: str
    profiles: list[str] = Field(default_factory=lambda: ["default"])
    session: SessionConfig
    rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    cache_seconds: int = Field(default=0, ge=0, le=3600)
    redact: list[str] = Field(default_factory=list)
    tools: list[ToolConfig] = Field(default_factory=list)
    hooks: str | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError(f"invalid service name {value!r}")
        return value

    @field_validator("profiles")
    @classmethod
    def _valid_profiles(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one profile is required")
        for profile in value:
            if not NAME_RE.match(profile):
                raise ValueError(f"invalid profile name {profile!r}")
        return value

    @model_validator(mode="after")
    def _unique_tools(self) -> Manifest:
        names = [tool.name for tool in self.tools]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate tool names: {sorted(duplicates)}")
        return self


class ManifestLoadResult(BaseModel):
    manifests: dict[str, Manifest] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


def load_manifest_file(path: Path) -> Manifest:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be a YAML mapping")
    manifest = Manifest.model_validate(data)
    if path.stem != manifest.name:
        raise ValueError(f"file name {path.stem!r} does not match service name {manifest.name!r}")
    return manifest


def load_manifests(services_dir: Path) -> ManifestLoadResult:
    result = ManifestLoadResult()
    if not services_dir.exists():
        return result
    for path in sorted(services_dir.glob("*.yaml")):
        try:
            manifest = load_manifest_file(path)
        except Exception as exc:  # isolate: one broken manifest must not affect the others
            result.errors[path.stem] = f"{type(exc).__name__}: {exc}"
            continue
        if manifest.enabled:
            result.manifests[manifest.name] = manifest
    return result
