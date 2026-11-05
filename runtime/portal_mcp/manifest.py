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


class CsrfPage(BaseModel):
    """A page on the base host whose hidden input or meta tag holds the CSRF token."""

    path: str
    name: str  # name of the hidden input (value=...) or meta tag (content=...)

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "://" in value:
            raise ValueError("csrf.from_page.path must be a path on the base_url host")
        return value


class CsrfConfig(BaseModel):
    """Where the CSRF token comes from (a cookie or a page) and where it goes (a header or a
    body field)."""

    header: str | None = None
    from_cookie: str | None = None
    from_page: CsrfPage | None = None
    body_field: str | None = None

    @model_validator(mode="after")
    def _one_source_and_a_target(self) -> CsrfConfig:
        if (self.from_cookie is None) == (self.from_page is None):
            raise ValueError("csrf needs exactly one of from_cookie and from_page")
        if self.header is None and self.body_field is None:
            raise ValueError("csrf needs a header or a body_field")
        return self


class DataAuth(BaseModel):
    """How tools on a data host authenticate: with the token the service's own app gets after
    login. The app keeps it in the browser's storage; the runtime opens `page` after the login,
    reads the token from `source` and sends it only to declared data hosts."""

    model_config = ConfigDict(extra="forbid")
    source: str  # "sessionStorage:<key>" or "localStorage:<key>"; "*" matches any characters
    field: str | None = None  # dotted path in a JSON value, e.g. "access_token"
    page: str  # path on the base host that makes the app get its token

    @field_validator("source")
    @classmethod
    def _valid_source(cls, value: str) -> str:
        kind, _, key = value.partition(":")
        if kind not in ("localStorage", "sessionStorage") or not key:
            raise ValueError("data_auth.source must be 'sessionStorage:<key>' or 'localStorage:<key>'")
        return value

    @field_validator("page")
    @classmethod
    def _valid_page(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "://" in value:
            raise ValueError("data_auth.page must be a path on the base_url host")
        return value


class SessionConfig(BaseModel):
    # "none": a public site. No login, no session check, nothing stored.
    type: Literal["cookies", "bearer", "none"] = "cookies"
    bearer_source: str | None = None  # e.g. "localStorage:auth_token"
    csrf: CsrfConfig | None = None
    check: str | None = None  # path; 200 means the session is valid, 401 means it is not
    logout: str | None = None  # path, called with POST

    @model_validator(mode="after")
    def _bearer_needs_source(self) -> SessionConfig:
        if self.type == "bearer" and not self.bearer_source:
            raise ValueError("session.bearer_source is required when session.type is 'bearer'")
        if self.type == "none":
            if self.check or self.logout or self.csrf or self.bearer_source:
                raise ValueError("session.type 'none' takes no check, logout, csrf or bearer_source")
            return self
        if not self.check:
            raise ValueError("session.check is required unless session.type is 'none'")
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


class ExtractField(BaseModel):
    """One named field of an HTML extraction recipe.

    `select` is a CSS selector, relative to the page or to the enclosing item. Without
    `fields`, the field is the element's text (or the value of `attr`); with `many`, a list
    of them. With `fields`, each matching element becomes an object (a list, unless `many`
    is false)."""

    model_config = ConfigDict(extra="forbid")
    select: str
    attr: str | None = None
    many: bool | None = None
    fields: dict[str, ExtractField] | None = None

    @model_validator(mode="after")
    def _shape(self) -> ExtractField:
        if not self.select.strip():
            raise ValueError("extract: select must be a CSS selector")
        if self.fields is not None and self.attr is not None:
            raise ValueError("extract: a field with fields cannot also have attr")
        if self.fields is not None and not self.fields:
            raise ValueError("extract: fields must name at least one field")
        return self


class EmbeddedJson(BaseModel):
    """JSON a page carries in a <script> tag, for example a framework's page data
    (script#__NEXT_DATA__) or structured data (script[type='application/ld+json'])."""

    model_config = ConfigDict(extra="forbid")
    select: str  # CSS selector for the script tag
    path: str | None = None  # dotted path into the JSON, e.g. "props.pageProps.result"
    many: bool = False  # all matching tags, as a list


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
    body_encoding: Literal["json", "form", "multipart"] = "json"
    # multipart: body fields that are files. Their arguments are file paths inside the
    # portal-mcp uploads directory.
    file_fields: list[str] = Field(default_factory=list)
    body_constants: dict[str, Any] = Field(default_factory=dict)  # fixed fields, e.g. hidden form inputs
    summary: str | None = None  # confirmation text template, e.g. "Pay {amount} to {recipient}"
    limits: Limits = Field(default_factory=Limits)
    messaging: bool = False  # sends free text to other people: off unless enabled is true
    enabled: bool | None = None
    test_args: dict[str, Any] | None = None  # arguments self_test uses for read tools
    # A declared data host to call instead of the base host, with the token from data_auth.
    host: str | None = None
    # A read or file tool that needs no login, in a service that otherwise does.
    public: bool = False
    # Read tools on HTML pages: the answer is text/html, turned into JSON by `embedded`
    # (JSON in a script tag) or `extract` (CSS selectors). Scripts are never run.
    response: Literal["json", "html"] = "json"
    embedded: EmbeddedJson | None = None
    extract: dict[str, ExtractField] | None = None

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
        if self.response == "html":
            if self.kind != "read":
                raise ValueError(f"tool {self.name!r}: response html is only for read tools")
            if (self.embedded is None) == (self.extract is None):
                raise ValueError(f"tool {self.name!r}: response html needs exactly one of embedded and extract")
            if self.extract is not None and not self.extract:
                raise ValueError(f"tool {self.name!r}: extract must name at least one field")
        elif self.embedded is not None or self.extract is not None:
            raise ValueError(f"tool {self.name!r}: embedded and extract need response: html")
        if self.public and self.is_write:
            raise ValueError(f"write tool {self.name!r} cannot be public")
        if self.file_fields and self.body_encoding != "multipart":
            raise ValueError(f"tool {self.name!r}: file_fields need body_encoding multipart")
        if self.kind == "binding" and self.limits.is_empty():
            raise ValueError(f"binding tool {self.name!r} needs limits, for example a maximum amount")
        if self.messaging and not self.is_write:
            raise ValueError("messaging only applies to write tools")
        return self


class Manifest(BaseModel):
    name: str
    enabled: bool = True
    base_url: str
    login_url: str | None = None  # required unless session.type is "none"
    profiles: list[str] = Field(default_factory=lambda: ["default"])
    session: SessionConfig  # required, also for public sites: `session: {type: none}`
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
    # Hosts the pages load static files from (scripts, styles, fonts, images), for example a
    # CDN with a library the service needs. GET only, https only, never data or navigation.
    asset_hosts: list[str] = Field(default_factory=list)
    # After login, login hosts are blocked. "oidc" opens them only for the standard OpenID
    # Connect endpoints that the discovery document names, for apps that get their own token.
    login_hosts_after_login: Literal["blocked", "oidc"] = "blocked"
    # The token tools on data hosts send. Needs data_hosts and a login.
    data_auth: DataAuth | None = None

    @field_validator("login_hosts")
    @classmethod
    def _valid_login_hosts(cls, value: list[str]) -> list[str]:
        return _validate_hosts(value, "login host")

    @field_validator("data_hosts")
    @classmethod
    def _valid_data_hosts(cls, value: list[str]) -> list[str]:
        return _validate_hosts(value, "data host")

    @field_validator("asset_hosts")
    @classmethod
    def _valid_asset_hosts(cls, value: list[str]) -> list[str]:
        from .diagnostics import is_tracking_host

        value = _validate_hosts(value, "asset host")
        tracking = [h for h in value if is_tracking_host(h)]
        if tracking:
            raise ValueError(f"tracking hosts cannot be asset hosts: {tracking}")
        return value

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

    @property
    def needs_login(self) -> bool:
        return self.session.type != "none"

    @model_validator(mode="after")
    def _login_settings(self) -> Manifest:
        if self.needs_login and not self.login_url:
            raise ValueError("login_url is required unless session.type is 'none'")
        if not self.needs_login and (self.login_hosts or self.login_hosts_after_login != "blocked"):
            raise ValueError("a service without login (session.type 'none') cannot have login hosts")
        return self

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
        if service_host in self.asset_hosts:
            raise ValueError("asset_hosts must not contain the base_url host; it is allowed already")
        lists = {"login_hosts": self.login_hosts, "data_hosts": self.data_hosts, "asset_hosts": self.asset_hosts}
        names = list(lists)
        for i, first in enumerate(names):
            for second in names[i + 1:]:
                both = sorted(set(lists[first]) & set(lists[second]))
                if both:
                    raise ValueError(f"a host cannot be in both {first} and {second}: {both}")
        if self.login_hosts_after_login == "oidc" and not self.login_hosts:
            raise ValueError("login_hosts_after_login: oidc needs at least one login host")
        if self.data_auth is not None and (not self.data_hosts or not self.needs_login):
            raise ValueError("data_auth needs data_hosts and a login")
        for tool in self.tools:
            if tool.host is None:
                continue
            if tool.host not in self.data_hosts:
                raise ValueError(f"tool {tool.name!r}: host {tool.host!r} is not a declared data host")
            if self.data_auth is None:
                raise ValueError(f"tool {tool.name!r} calls a data host, which needs data_auth")
            if tool.public:
                raise ValueError(f"tool {tool.name!r} on a data host cannot be public")
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
