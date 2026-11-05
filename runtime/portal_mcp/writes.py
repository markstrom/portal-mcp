"""Write and binding tools: validation, limits, one-time codes, human approval and the audit log.

Approval must come from a person, never from the model. Service data can contain text
written by others, and a manipulated model must not be able to approve its own actions.
So `confirm_<tool>` only runs after:
- an MCP elicitation answered by the user in the client, when the client supports it, or
- a click on a local confirmation page that the runtime opens in the browser. The page
  address carries a secret token that is never returned to the model.
"""

from __future__ import annotations

import asyncio
import copy
import html
import json
import logging
import os
import re
import secrets
import socket
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import jsonschema

from .manifest import ToolConfig
from .notify import notify
from .service import PATH_PARAM, ServiceError

log = logging.getLogger("portal_mcp.writes")

CODE_TTL_SECONDS = 300  # a prepared action must be confirmed within five minutes
APPROVAL_WAIT_SECONDS = 20.0  # how long a confirm call waits for a click before returning

Status = Literal["pending", "approved", "declined"]


class WriteError(ServiceError):
    pass


# ----- validation -----------------------------------------------------------

def path_params(tool: ToolConfig) -> list[str]:
    return PATH_PARAM.findall(tool.path)


def write_input_schema(tool: ToolConfig) -> dict[str, Any]:
    """Path parameters plus body fields. Unknown fields are rejected."""
    properties: dict[str, Any] = {}
    required: set[str] = set()
    for schema in (tool.params or {}, tool.body_schema or {}):
        properties.update(copy.deepcopy(schema.get("properties", {})))
        required.update(schema.get("required", []))
    for name in path_params(tool):
        properties.setdefault(name, {"type": "string"})
        required.add(name)
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
    }


def _get(data: dict[str, Any], dotted: str) -> Any:
    value: Any = data
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def validate_write(tool: ToolConfig, arguments: dict[str, Any]) -> None:
    """Raise WriteError if the arguments do not match the schema or break a limit."""
    try:
        jsonschema.validate(arguments, write_input_schema(tool))
    except jsonschema.ValidationError as exc:
        raise WriteError(f"invalid input for {tool.name}: {exc.message}") from exc

    limits = tool.limits
    for dotted, maximum in limits.max.items():
        value = _get(arguments, dotted)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise WriteError(f"{dotted} must be a number")
        if value > maximum:
            raise WriteError(f"{dotted} is {value}, above the limit of {maximum}")
    for dotted, minimum in limits.min.items():
        value = _get(arguments, dotted)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise WriteError(f"{dotted} must be a number")
        if value < minimum:
            raise WriteError(f"{dotted} is {value}, below the limit of {minimum}")
    for dotted, allowed in limits.allowed.items():
        value = _get(arguments, dotted)
        if value is not None and value not in allowed:
            raise WriteError(f"{dotted} must be one of {allowed}")
    for dotted, max_length in limits.max_length.items():
        value = _get(arguments, dotted)
        if isinstance(value, str) and len(value) > max_length:
            raise WriteError(f"{dotted} is longer than {max_length} characters")


_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_summary(service: str, tool: ToolConfig, arguments: dict[str, Any], profile: str,
                  *, files: dict[str, str] | None = None) -> str:
    """The text a person approves. Built by the runtime from the real arguments."""
    if tool.summary:
        headline = _PLACEHOLDER.sub(lambda m: _short(arguments.get(m.group(1), "<missing>"), 120), tool.summary)
    else:
        headline = tool.description or tool.name
    lines = [
        headline,
        "",
        f"Service: {service} (profile {profile})",
        f"Request: {tool.method} {('https://' + tool.host) if tool.host else ''}{tool.path}",
        f"Kind: {tool.kind}",
    ]
    if arguments:
        lines.append("Values:")
        lines.extend(f"  {key}: {_short(value)}" for key, value in sorted(arguments.items()))
    if files:
        lines.append("Files sent:")
        lines.extend(f"  {key}: {value}" for key, value in sorted(files.items()))
    if tool.body_constants:
        lines.append("Fixed values:")
        lines.extend(f"  {key}: {_short(value)}" for key, value in sorted(tool.body_constants.items()))
    return "\n".join(lines)


# ----- pending actions ------------------------------------------------------

@dataclass
class PendingAction:
    code: str
    service: str
    tool: str
    profile: str
    arguments: dict[str, Any]
    summary: str
    expires: float
    status: Status = "pending"
    page_token: str | None = None
    executing: bool = False
    decided: asyncio.Event = field(default_factory=asyncio.Event)

    def decide(self, status: Status) -> None:
        if self.status == "pending":
            self.status = status
            self.decided.set()


class PendingActions:
    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = CODE_TTL_SECONDS):
        self._clock = clock
        self._ttl = ttl
        self._by_code: dict[str, PendingAction] = {}

    def _purge(self) -> None:
        now = self._clock()
        for code in [c for c, a in self._by_code.items() if a.expires <= now]:
            del self._by_code[code]

    def create(self, service: str, tool: str, profile: str, arguments: dict[str, Any], summary: str) -> PendingAction:
        self._purge()
        code = secrets.token_hex(4).upper()
        while code in self._by_code:
            code = secrets.token_hex(4).upper()
        action = PendingAction(code, service, tool, profile, arguments, summary, self._clock() + self._ttl)
        self._by_code[code] = action
        return action

    def get(self, code: Any, service: str, tool: str) -> PendingAction:
        self._purge()
        action = self._by_code.get(str(code).strip().upper()) if code else None
        if action is None:
            raise WriteError("unknown or expired code; call prepare again")
        if action.service != service or action.tool != tool:
            raise WriteError("this code belongs to another tool")
        return action

    def by_token(self, token: str) -> PendingAction | None:
        self._purge()
        for action in self._by_code.values():
            if action.page_token is not None and secrets.compare_digest(action.page_token, token):
                return action
        return None

    def discard(self, code: str) -> None:
        self._by_code.pop(code, None)

    def remaining_seconds(self, action: PendingAction) -> int:
        return max(int(action.expires - self._clock()), 0)


# ----- audit log ------------------------------------------------------------

class AuditLog:
    """JSON lines with time, service, tool, call and response. Mode 0600."""

    def __init__(self, path: Path):
        self._path = path

    def write(self, event: str, **fields: Any) -> None:
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


# ----- browser approval -----------------------------------------------------

_PAGE = """<!doctype html>
<html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bekräfta åtgärd</title>
<style>
:root {{ color-scheme: light dark; --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --box:#f5f5f7; --ok:#0a7d32; --no:#b3261e; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1c1e; --fg:#f5f5f7; --muted:#a1a1a6; --box:#2c2c2e; }} }}
body {{ font: 16px/1.5 system-ui, sans-serif; background: var(--bg); color: var(--fg); margin: 0; padding: 24px 16px; }}
main {{ max-width: 640px; margin: 0 auto; }}
pre {{ background: var(--box); padding: 16px; border-radius: 8px; white-space: pre-wrap; word-break: break-word; }}
p.note {{ color: var(--muted); }}
.buttons {{ display: flex; gap: 12px; flex-wrap: wrap; }}
button {{ font: inherit; padding: 10px 20px; border-radius: 8px; border: 0; color: #fff; cursor: pointer; }}
.ok {{ background: var(--ok); }} .no {{ background: var(--no); }}
</style></head>
<body><main>
<h1>{title}</h1>
{body}
</main></body></html>"""


class BrowserApproval:
    """Serves a local confirmation page on 127.0.0.1 and waits for a click."""

    def __init__(
        self,
        pending: PendingActions,
        opener: Callable[[str], Any] = webbrowser.open,
        wait_seconds: float = APPROVAL_WAIT_SECONDS,
    ):
        self._pending = pending
        self._opener = opener
        self._wait = wait_seconds
        self._port: int | None = None
        self._task: asyncio.Task | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    async def _ensure_started(self) -> None:
        if self._task is not None and not self._task.done():
            return
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self._port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(self._app(), log_level="warning", lifespan="off"))
        self._task = asyncio.create_task(server.serve(sockets=[sock]), name="confirmation-page")
        for _ in range(200):
            if server.started:
                return
            await asyncio.sleep(0.01)

    def url_for(self, action: PendingAction) -> str:
        return f"{self.origin}/confirm/{action.page_token}"

    async def request(self, action: PendingAction, wait_seconds: float | None = None) -> Status:
        wait_seconds = self._wait if wait_seconds is None else wait_seconds
        await self._ensure_started()
        if action.page_token is None:
            action.page_token = secrets.token_urlsafe(32)
            self._opener(self.url_for(action))
            await notify("Bekräftelse väntar", f"{action.service}: godkänn eller avbryt åtgärden i webbläsaren.")
        if action.status == "pending":
            try:
                await asyncio.wait_for(action.decided.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
        return action.status

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _app(self):
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import HTMLResponse
        from starlette.routing import Route

        headers = {
            "cache-control": "no-store",
            "x-frame-options": "DENY",
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
            "referrer-policy": "no-referrer",
        }

        def page(title: str, body: str, status: int = 200) -> HTMLResponse:
            return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status, headers=headers)

        async def show(request: Request):
            action = self._pending.by_token(request.path_params["token"])
            if action is None:
                return page("Okänd eller utgången begäran", "<p>Förbered åtgärden igen.</p>", 404)
            if action.status != "pending":
                return page("Redan besvarad", f"<p>Status: {html.escape(action.status)}</p>")
            remaining = self._pending.remaining_seconds(action)
            body = (
                "<p>En åtgärd väntar på ditt godkännande. Kontrollera uppgifterna nedan.</p>"
                f"<pre>{html.escape(action.summary)}</pre>"
                f"<p class=\"note\">Kod {html.escape(action.code)}. Gäller i {remaining // 60} min {remaining % 60} s.</p>"
                "<form method=\"post\" class=\"buttons\">"
                "<button class=\"ok\" name=\"decision\" value=\"approve\">Bekräfta</button>"
                "<button class=\"no\" name=\"decision\" value=\"decline\">Avbryt</button>"
                "</form>"
            )
            return page("Bekräfta åtgärd", body)

        async def decide(request: Request):
            origin = request.headers.get("origin")
            if origin is not None and origin != self.origin:
                return page("Nekad", "<p>Fel ursprung.</p>", 403)
            action = self._pending.by_token(request.path_params["token"])
            if action is None:
                return page("Okänd eller utgången begäran", "<p>Förbered åtgärden igen.</p>", 404)
            form = await request.form()
            decision = form.get("decision")
            if decision == "approve":
                action.decide("approved")
                return page("Godkänd", "<p>Åtgärden utförs när modellen anropar confirm. Du kan stänga fönstret.</p>")
            action.decide("declined")
            return page("Avbruten", "<p>Inget skickades. Du kan stänga fönstret.</p>")

        return Starlette(routes=[
            Route("/confirm/{token}", show, methods=["GET"]),
            Route("/confirm/{token}", decide, methods=["POST"]),
        ])


# ----- elicitation approval -------------------------------------------------

ELICIT_SCHEMA = {
    "type": "object",
    "properties": {
        "confirm": {
            "type": "boolean",
            "title": "Bekräfta",
            "description": "Kryssa i för att utföra åtgärden.",
        }
    },
    "required": ["confirm"],
}


async def elicit_approval(session: Any, action: PendingAction, related_request_id: Any = None) -> Status:
    """Ask the user through the client. Only an explicit accept with confirm=true approves."""
    result = await session.elicit_form(
        message=f"{action.summary}\n\nVill du utföra åtgärden?",
        requestedSchema=ELICIT_SCHEMA,
        related_request_id=related_request_id,
    )
    if result.action == "accept" and isinstance(result.content, dict) and result.content.get("confirm") is True:
        action.decide("approved")
    else:
        action.decide("declined")
    return action.status


def client_supports_elicitation(session: Any) -> bool:
    if os.environ.get("PORTAL_MCP_APPROVAL") == "browser":
        return False
    if session is None:
        return False
    from mcp import types

    try:
        return bool(session.check_client_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability())))
    except Exception:
        return False
