"""Manifest schema and loading. One YAML manifest per service in the services directory.

A broken manifest is reported as an error for that service only; other services still load.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# A DNS host name: labels of letters, digits and hyphens, with at least one dot.
HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


def _validate_hosts(value: list[str], label: str) -> list[str]:
    seen: set[str] = set()
    for host in value:
        if not isinstance(host, str) or host != host.lower():
            raise ValueError(f"{label} {host!r} must be lowercase")
        if not HOST_RE.match(host):
            raise ValueError(f"{label} {host!r} must be a plain host name, without scheme, path or port")
        if host == "localhost" or host.endswith(".localhost") or _is_ip(host):
            raise ValueError(f"{label} {host!r} must be a public host name, not localhost or an IP address")
        if host in seen:
            raise ValueError(f"{label} {host!r} is listed twice")
        seen.add(host)
    return value


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
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


class Limits(BaseModel):
    """Bounds on arguments of write and binding tools. Keys are dotted argument paths."""

    model_config = ConfigDict(extra="forbid")

    max: dict[str, float] = Field(default_factory=dict)  # e.g. {amount: 5000}
    min: dict[str, float] = Field(default_factory=dict)
    allowed: dict[str, list[Any]] = Field(default_factory=dict)  # e.g. {currency: [SEK]}
    max_length: dict[str, int] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.max or self.min or self.allowed or self.max_length)


class ToolConfig(BaseModel):
    name: str
    kind: ToolKind
    description: str = ""
    method: HttpMethod = "GET"
    path: str
    # Arguments. For read and file tools: path and query parameters.
    # For write and binding tools: path parameters (body fields go in body_schema).
    params: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    pick: list[str] | None = None  # keep only these dotted field paths in JSON responses
    # Write and binding tools
    body_schema: dict[str, Any] | None = None  # JSON Schema for the request body
    body_encoding: Literal["json", "form"] = "json"
    body_constants: dict[str, Any] = Field(default_factory=dict)  # fixed fields, e.g. hidden form inputs
    summary: str | None = None  # confirmation text template, e.g. "Pay {amount} to {recipient}"
    limits: Limits = Field(default_factory=Limits)
    messaging: bool = False  # sends free text to other people: off unless enabled is true
    enabled: bool | None = None
    test_args: dict[str, Any] | None = None  # arguments self_test uses for read tools

    @property
    def exposed(self) -> bool:
        if self.enabled is False:
            return False
        if self.messaging and self.enabled is not True:
            return False
        return True

    @property
    def is_write(self) -> bool:
        return self.kind in ("write", "binding")

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
        if value.startswith("//") or "://" in value:
            raise ValueError("tool path must stay on the base_url host")
        return value

    @model_validator(mode="after")
    def _kind_rules(self) -> ToolConfig:
        if self.kind in ("read", "file") and self.method != "GET":
            raise ValueError(f"{self.kind} tool {self.name!r} must use GET")
        if self.is_write:
            if self.method == "GET":
                raise ValueError(f"write tool {self.name!r} cannot use GET")
            if self.method != "DELETE" and self.body_schema is None:
                raise ValueError(f"write tool {self.name!r} needs body_schema")
        if self.kind == "binding" and self.limits.is_empty():
            raise ValueError(f"binding tool {self.name!r} needs limits, for example a maximum amount")
        if self.messaging and not self.is_write:
            raise ValueError("messaging only applies to write tools")
        return self


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
    # Other hosts the login goes through (identity provider, BankID gateway, account service).
    # Approved by the user. Allowed during login only, over https, never sent the session.
    login_hosts: list[str] = Field(default_factory=list)
    # Other hosts the service's pages load data from (an API on another host). Approved by the
    # user. In recordings: GET over https; writes to them are captured, not sent. They never
    # get the session. Runtime tools that call them are not supported yet.
    data_hosts: list[str] = Field(default_factory=list)

    @field_validator("login_hosts")
    @classmethod
    def _valid_login_hosts(cls, value: list[str]) -> list[str]:
        return _validate_hosts(value, "login host")

    @field_validator("data_hosts")
    @classmethod
    def _valid_data_hosts(cls, value: list[str]) -> list[str]:
        return _validate_hosts(value, "data host")

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

    @model_validator(mode="after")
    def _hosts_differ_from_service(self) -> Manifest:
        service_host = (urlsplit(self.base_url).hostname or "").lower()
        if service_host in self.login_hosts:
            raise ValueError("login_hosts must not contain the base_url host; it is allowed already")
        if service_host in self.data_hosts:
            raise ValueError("data_hosts must not contain the base_url host; it is allowed already")
        both = sorted(set(self.login_hosts) & set(self.data_hosts))
        if both:
            raise ValueError(f"a host cannot be both a login host and a data host: {both}")
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
