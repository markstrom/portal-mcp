"""Turn a recorded HAR file into a candidate tool catalog.

Output contains only structure: path templates, parameter names, pagination hints and
response field names with types. Response values are never written out.

Usage:
  uv run --project "$CLAUDE_PLUGIN_ROOT" python analyze.py recording.har --host app.example.com > catalog.json
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

PAGINATION_PARAMS = {"page", "pageindex", "page_index", "offset", "cursor", "after", "before", "limit", "pagesize", "page_size", "per_page", "perpage"}
SENSITIVE_WORDS = (
    "national", "personnummer", "ssn", "address", "adress", "email", "phone", "telefon", "mobile",
    "iban", "account", "konto", "birth", "födel", "passport", "tax", "salary", "lön", "card", "kort",
)
ID_SEGMENT = re.compile(r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-fA-F]{16,}|[a-z]{2,4}-\d+)$")
MAX_DEPTH = 4


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


def schema_of(value, depth: int = 0):
    """Structure only: field names and JSON types, never values."""
    if depth > MAX_DEPTH:
        return "..."
    if isinstance(value, dict):
        return {k: schema_of(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return ["empty"]
        return [schema_of(value[0], depth + 1)]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


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


def suggest_name(template: str) -> str:
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", template) if w and not w.startswith("{")]
    name = "_".join(w.lower() for w in words) or "root"
    return re.sub(r"_+", "_", name).strip("_")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build a candidate tool catalog from a HAR file.")
    parser.add_argument("har", type=Path)
    parser.add_argument("--host", required=True, help="the service host, for example app.example.com")
    args = parser.parse_args(argv)
    har = json.loads(args.har.read_text(encoding="utf-8"))
    json.dump(analyze(har, args.host), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
