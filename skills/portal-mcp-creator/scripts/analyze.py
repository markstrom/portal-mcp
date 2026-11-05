"""Turn a recorded HAR file into a candidate tool catalog.

Output contains only structure: path templates, parameter names, pagination hints and
response field names with types. Response values are never written out. With --writes,
captured (aborted) write requests become write candidates; their values are hidden too,
except for fields named in --show-values-for (meant for fixed fields such as hidden inputs).

Usage:
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python analyze.py recording.har --host app.example.com > catalog.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import side_effect_hint  # noqa: E402

from portal_mcp.schema import schema_of  # noqa: E402,F401  (shared with self_test)

PAGINATION_PARAMS = {"page", "pageindex", "page_index", "offset", "cursor", "after", "before", "limit", "pagesize", "page_size", "per_page", "perpage"}
SENSITIVE_WORDS = (
    "national", "personnummer", "ssn", "address", "adress", "email", "phone", "telefon", "mobile",
    "iban", "account", "konto", "birth", "födel", "passport", "tax", "salary", "lön", "card", "kort",
)
ID_SEGMENT = re.compile(r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-fA-F]{16,}|[a-z]{2,4}-\d+)$")


def path_template(path: str) -> str:
    out = []
    for index, segment in enumerate(path.split("/")):
        if segment and ID_SEGMENT.match(segment):
            prev = out[-1] if out else ""
            name = f"{prev.rstrip('s') or 'item'}_id" if prev else "id"
            out.append("{" + re.sub(r"[^a-z0-9_]", "_", name.lower()) + "}")
        else:
            out.append(segment)
    return "/".join(out)


def sensitive_fields(schema, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(schema, dict):
        for key, sub in schema.items():
            name = f"{prefix}{key}"
            if any(word in key.lower() for word in SENSITIVE_WORDS):
                found.append(name)
            found.extend(sensitive_fields(sub, name + "."))
    elif isinstance(schema, list) and schema:
        found.extend(sensitive_fields(schema[0], prefix))
    return found


def analyze(har: dict, host: str) -> dict:
    groups: dict[str, dict] = {}
    skipped = defaultdict(int)
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        response = entry.get("response", {})
        url = request.get("url", "")
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != host.lower():
            skipped["other host"] += 1
            continue
        if request.get("method", "").upper() != "GET":
            skipped["not GET"] += 1
            continue
        hint = side_effect_hint(parts.path)
        if hint is not None:
            skipped[f"side effect: {hint}"] += 1
            continue
        mime = (response.get("content", {}).get("mimeType") or "").lower()
        status = response.get("status", 0)
        if status != 200:
            skipped[f"status {status}"] += 1
            continue

        template = path_template(parts.path)
        group = groups.setdefault(template, {
            "method": "GET",
            "path": template,
            "raw_paths": set(),
            "query_params": set(),
            "pagination_params": set(),
            "response_kind": None,
            "schema": None,
            "examples": 0,
        })
        group["raw_paths"].add(parts.path)
        for name, _ in parse_qsl(parts.query, keep_blank_values=True):
            group["query_params"].add(name)
            if name.lower() in PAGINATION_PARAMS:
                group["pagination_params"].add(name)

        if "json" in mime:
            body = response.get("content", {}).get("text") or ""
            try:
                data = json.loads(body) if body else None
            except json.JSONDecodeError:
                skipped["invalid JSON"] += 1
                continue
            group["response_kind"] = "json"
            group["examples"] += 1
            if group["schema"] is None:
                group["schema"] = schema_of(data)
        elif "pdf" in mime:
            group["response_kind"] = "file"
            group["examples"] += 1
        else:
            skipped[f"not JSON or PDF ({mime or 'no type'})"] += 1

    tools = []
    for template, group in sorted(groups.items()):
        if group["response_kind"] is None:
            continue
        schema = group["schema"]
        kind = "file" if group["response_kind"] == "file" else "read"
        tools.append({
            "suggested_name": suggest_name(template),
            "kind": kind,
            "method": "GET",
            "path": template,
            "raw_paths": sorted(group["raw_paths"]),
            "query_params": sorted(group["query_params"]),
            "pagination_params": sorted(group["pagination_params"]),
            "response_schema": schema,
            "sensitive_fields": sensitive_fields(schema) if schema is not None else [],
            "examples_seen": group["examples"],
        })
    return {"host": host, "tools": tools, "skipped": dict(skipped)}


MESSAGING_WORDS = ("message", "meddelande", "chat", "mail", "comment", "kommentar", "reply", "svar", "text", "body", "subject", "ämne", "note")
BINDING_WORDS = ("amount", "belopp", "price", "pris", "sum", "summa", "pay", "betal", "transfer", "överför", "bid", "bud", "order", "beställ", "sign", "contract", "avtal", "quantity", "antal")
TOKEN_WORDS = ("csrf", "xsrf", "token", "authenticity", "nonce", "signature", "_token")


def _body_fields(content_type: str, body: str) -> tuple[str, dict]:
    """Return (encoding, {field: value}) for a captured body."""
    content_type = content_type.lower()
    if "json" in content_type:
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return "json-invalid", {}
        return "json", data if isinstance(data, dict) else {"_root": data}
    if "x-www-form-urlencoded" in content_type:
        fields: dict = {}
        for name, value in parse_qsl(body, keep_blank_values=True):
            if name in fields:
                existing = fields[name]
                fields[name] = existing + [value] if isinstance(existing, list) else [existing, value]
            else:
                fields[name] = value
        return "form", fields
    if "multipart" in content_type:
        return "multipart", {}
    return content_type or "none", {}


def _form_type(value) -> str:
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and re.fullmatch(r"-?\d+", stripped):
            return "integer (as text)"
        if stripped and re.fullmatch(r"-?\d+[.,]\d+", stripped):
            return "number (as text)"
        return "string"
    return schema_of(value) if not isinstance(value, dict) else "object"


def analyze_writes(captured: list[dict], host: str, show_values_for: set[str] | None = None) -> list[dict]:
    """Candidate write tools from captured, aborted requests. Values are hidden by default."""
    show_values_for = show_values_for or set()
    groups: dict[tuple[str, str], dict] = {}
    for item in captured:
        method = item.get("method", "POST").upper()
        template = path_template(item.get("path", "/"))
        encoding, fields = _body_fields(item.get("content_type", ""), item.get("body", ""))
        group = groups.setdefault((method, template), {
            "method": method,
            "path": template,
            "encoding": encoding,
            "fields": {},
            "seen_in": [],
            "captures": 0,
            "pages": set(),
            "values": {},
        })
        group["captures"] += 1
        group["seen_in"].append(set(fields))
        if item.get("page"):
            group["pages"].add(item["page"])
        for name, value in fields.items():
            group["fields"].setdefault(name, _form_type(value) if encoding == "form" else schema_of(value))
            if name in show_values_for:
                group["values"].setdefault(name, value)

    out = []
    for (method, template), group in sorted(groups.items()):
        names = list(group["fields"])
        always = set.intersection(*group["seen_in"]) if group["seen_in"] else set()
        lowered = [n.lower() for n in names] + [w for w in re.split(r"[^a-z]+", template.lower()) if w]
        messaging = sorted({w for w in MESSAGING_WORDS for n in lowered if w in n})
        binding = sorted({w for w in BINDING_WORDS for n in lowered if w in n})
        tokens = sorted(n for n in names if any(w in n.lower() for w in TOKEN_WORDS))
        out.append({
            "suggested_name": suggest_name(template),
            "suggested_kind": "binding" if binding else "write",
            "method": method,
            "path": template,
            "encoding": group["encoding"],
            "fields": group["fields"],
            "present_in_every_capture": sorted(always),
            "token_fields": tokens,
            "messaging_hints": messaging,
            "binding_hints": binding,
            "sensitive_fields": [n for n in names if any(w in n.lower() for w in SENSITIVE_WORDS)],
            "captures": group["captures"],
            "pages": sorted(group["pages"]),
            "values": group["values"],
        })
    return out


def suggest_name(template: str) -> str:
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", template) if w and not w.startswith("{")]
    name = "_".join(w.lower() for w in words) or "root"
    return re.sub(r"_+", "_", name).strip("_")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build a candidate tool catalog from a HAR file.")
    parser.add_argument("har", type=Path)
    parser.add_argument("--host", required=True, help="the service host, for example app.example.com")
    parser.add_argument("--writes", type=Path, help="captured_writes.json from record.py")
    parser.add_argument("--keep-har", action="store_true",
                        help="keep the HAR file; by default it is deleted once the catalog is written")
    parser.add_argument(
        "--show-values-for",
        default="",
        help="comma-separated field names whose captured values may be shown (for fixed fields only)",
    )
    args = parser.parse_args(argv)
    har = json.loads(args.har.read_text(encoding="utf-8"))
    catalog = analyze(har, args.host)
    if args.writes:
        captured = json.loads(args.writes.read_text(encoding="utf-8"))
        shown = {n.strip() for n in args.show_values_for.split(",") if n.strip()}
        catalog["writes"] = analyze_writes(captured, args.host, shown)
    json.dump(catalog, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()
    # The HAR holds cookies and page content. It is not needed once the catalog exists.
    if not args.keep_har:
        args.har.unlink(missing_ok=True)
        print(f"[portal-mcp] Deleted {args.har} (it held cookies and page content).", file=sys.stderr)


if __name__ == "__main__":
    main()
