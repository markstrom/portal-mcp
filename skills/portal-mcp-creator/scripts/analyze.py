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
from html_skeleton import skeleton  # noqa: E402

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


def analyze(har: dict, host: str, data_hosts: tuple[str, ...] | list[str] = ()) -> dict:
    """Read candidates for the service's host, plus declared data hosts (marked with their host)."""
    hosts = {host.lower(), *(h.lower() for h in data_hosts)}
    groups: dict[tuple[str, str], dict] = {}
    skipped = defaultdict(int)
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        response = entry.get("response", {})
        url = request.get("url", "")
        parts = urlsplit(url)
        entry_host = (parts.hostname or "").lower()
        if entry_host not in hosts:
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
        group = groups.setdefault((entry_host, template), {
            "host": entry_host,
            "method": "GET",
            "path": template,
            "raw_paths": set(),
            "query_params": set(),
            "pagination_params": set(),
            "response_kind": None,
            "schema": None,
            "html": None,
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
        elif "html" in mime:
            # An HTML page: a candidate for `response: html`, described without any text.
            if group["response_kind"] in ("json", "file"):
                continue
            group["response_kind"] = "html"
            group["examples"] += 1
            if group["html"] is None:
                body = _body_text(response.get("content", {}))
                try:
                    group["html"] = skeleton(body) if body else {"embedded_json": [], "repeated_elements": []}
                except Exception as exc:  # a page that cannot be parsed is reported, not fatal
                    group["html"] = {"error": type(exc).__name__}
        else:
            skipped[f"not JSON or PDF ({mime or 'no type'})"] += 1

    tools = []
    for (entry_host, template), group in sorted(groups.items(), key=lambda kv: (kv[0][0] != host.lower(), kv[0])):
        if group["response_kind"] is None:
            continue
        schema = group["schema"]
        kind = "file" if group["response_kind"] == "file" else "read"
        extra = {}
        if group["response_kind"] == "html":
            page = group["html"] or {}
            if not page.get("embedded_json") and not page.get("repeated_elements") and "error" not in page:
                skipped["HTML without embedded JSON or repeated elements"] += 1
                continue
            extra = {"response": "html", "html_skeleton": page}
        tools.append({
            "suggested_name": suggest_name(template),
            "host": entry_host,
            "data_host": entry_host != host.lower(),
            "kind": kind,
            "method": "GET",
            "path": template,
            "raw_paths": sorted(group["raw_paths"]),
            "query_params": sorted(group["query_params"]),
            "pagination_params": sorted(group["pagination_params"]),
            "response_schema": schema,
            "sensitive_fields": sensitive_fields(schema) if schema is not None else [],
            "examples_seen": group["examples"],
            **extra,
        })
    return {"host": host, "tools": tools, "skipped": dict(skipped)}


def _body_text(content: dict) -> str:
    text = content.get("text") or ""
    if content.get("encoding") == "base64":
        import base64

        try:
            return base64.b64decode(text).decode("utf-8", errors="replace")
        except ValueError:
            return ""
    return text


MESSAGING_WORDS = ("message", "meddelande", "chat", "mail", "comment", "kommentar", "reply", "svar", "text", "body", "subject", "ämne", "note")
BINDING_WORDS = ("amount", "belopp", "price", "pris", "sum", "summa", "pay", "betal", "transfer", "överför", "bid", "bud", "order", "beställ", "sign", "contract", "avtal", "quantity", "antal")
TOKEN_WORDS = ("csrf", "xsrf", "token", "authenticity", "nonce", "signature", "_token")


def _body_fields(content_type: str, body: str) -> tuple[str, dict]:
    """Return (encoding, {field: value}) for a captured body."""
    original = content_type  # the multipart boundary is case-sensitive
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
        return "multipart", _multipart_fields(original, body)
    return content_type or "none", {}


_DISPOSITION = re.compile(r'name="([^"]*)"(?:;\s*filename="([^"]*)")?', re.IGNORECASE)
FILE_MARK = "<file>"


def _multipart_fields(content_type: str, body: str) -> dict:
    """Text fields with their values, and file fields marked as files (their content is never kept)."""
    match = re.search(r"boundary=\"?([^\";]+)\"?", content_type, re.IGNORECASE)
    if not match or not body:
        return {}
    fields: dict = {}
    for part in body.split("--" + match.group(1)):
        head, _, value = part.partition("\r\n\r\n")
        if not _:
            head, _, value = part.partition("\n\n")
        found = _DISPOSITION.search(head)
        if not found:
            continue
        name, filename = found.group(1), found.group(2)
        fields[name] = FILE_MARK if filename is not None else value.rstrip("\r\n")
    return fields


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
    groups: dict[tuple[str, str, str], dict] = {}
    for item in captured:
        method = item.get("method", "POST").upper()
        item_host = (item.get("host") or host).lower()
        template = path_template(item.get("path", "/"))
        encoding, fields = _body_fields(item.get("content_type", ""), item.get("body", ""))
        group = groups.setdefault((item_host, method, template), {
            "host": item_host,
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
            if value == FILE_MARK:
                group["fields"].setdefault(name, "file")
                continue
            group["fields"].setdefault(name, _form_type(value) if encoding in ("form", "multipart") else schema_of(value))
            if name in show_values_for:
                group["values"].setdefault(name, value)

    out = []
    for (item_host, method, template), group in sorted(groups.items()):
        names = list(group["fields"])
        always = set.intersection(*group["seen_in"]) if group["seen_in"] else set()
        lowered = [n.lower() for n in names] + [w for w in re.split(r"[^a-z]+", template.lower()) if w]
        messaging = sorted({w for w in MESSAGING_WORDS for n in lowered if w in n})
        binding = sorted({w for w in BINDING_WORDS for n in lowered if w in n})
        tokens = sorted(n for n in names if any(w in n.lower() for w in TOKEN_WORDS))
        out.append({
            "suggested_name": suggest_name(template),
            "host": item_host,
            "data_host": item_host != host.lower(),
            "suggested_kind": "binding" if binding else "write",
            "method": method,
            "path": template,
            "encoding": group["encoding"],
            "fields": group["fields"],
            "file_fields": sorted(n for n, t in group["fields"].items() if t == "file"),
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


class HostAnalysisFailed(RuntimeError):
    pass


def _host_stats(har: dict, host: str) -> dict:
    requests = json_reads = 0
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        if (urlsplit(request.get("url", "")).hostname or "").lower() != host:
            continue
        requests += 1
        response = entry.get("response", {})
        mime = (response.get("content", {}).get("mimeType") or "").lower()
        if request.get("method", "").upper() == "GET" and response.get("status") == 200 and "json" in mime:
            json_reads += 1
    return {"requests": requests, "json_reads": json_reads}


def analyze_hosts(
    har: dict,
    service_host: str,
    data_hosts: tuple[str, ...] | list[str] = (),
    login_hosts: tuple[str, ...] | list[str] = (),
    *,
    analyze_one=None,
) -> dict:
    """One pass per declared host (the service's host and each data host).

    Login hosts are never data sources: their requests are only counted. Every catalog entry
    is marked with its host, and a `hosts` section says how each pass went.
    """
    analyze_one = analyze_one or (lambda h: analyze(har, h))
    service_host = service_host.lower()
    plan = [(service_host, "service")] + [(h.lower(), "data") for h in data_hosts]
    tools: list[dict] = []
    hosts: list[dict] = []
    skipped: dict[str, int] = {}
    for host, role in plan:
        report = {"host": host, "role": role, **_host_stats(har, host)}
        try:
            result = analyze_one(host)
        except Exception as exc:  # one host failing must not hide the others
            report.update(status="failed", error=type(exc).__name__, tools_found=0)
            hosts.append(report)
            continue
        for tool in result["tools"]:
            tool["host"] = host
            tool["data_host"] = role == "data"
        tools.extend(result["tools"])
        if role == "service":
            skipped = dict(result["skipped"])
        report["tools_found"] = len(result["tools"])
        if result["tools"]:
            report["status"] = "ok"
        elif report["requests"] == 0:
            report.update(status="empty", reason="no requests to this host in the recording")
        elif report["json_reads"] == 0:
            report.update(status="empty", reason="requests, but no JSON GET answers with status 200")
        else:
            report.update(status="empty", reason="JSON reads, but all of them were filtered out (see skipped)")
        hosts.append(report)

    # Requests to login hosts and data hosts are not "other host" for the service's pass.
    declared = {h.lower() for h in data_hosts}
    login = {h.lower() for h in login_hosts}
    login_requests = sum(_host_stats(har, h)["requests"] for h in login)
    data_requests = sum(_host_stats(har, h)["requests"] for h in declared)
    if "other host" in skipped:
        skipped["other host"] = max(skipped["other host"] - login_requests - data_requests, 0)
        if not skipped["other host"]:
            del skipped["other host"]
    if login_requests:
        skipped["login host (never a data source)"] = login_requests
    for h in sorted(login):
        hosts.append({"host": h, "role": "login", **_host_stats(har, h), "status": "not analyzed",
                      "reason": "login hosts are used only for logging in", "tools_found": 0})
    return {"host": service_host, "hosts": hosts, "tools": tools, "skipped": skipped,
            "failed_hosts": [r["host"] for r in hosts if r["status"] == "failed"]}


def _login_state(har_path: Path) -> bool | None:
    """True or False from the recorder's recording.json next to the HAR; None if it is missing.
    A public recording (no login needed) counts as True."""
    marker = har_path.parent / "recording.json"
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        # A public site needs no login, so its recording is accepted as it is.
        return bool(data.get("login_verified") or data.get("public"))
    except (OSError, ValueError):
        return None


def _hosts_from_manifest(args) -> tuple[str, list[str], list[str]] | None:
    if not (args.manifest or args.service):
        return None
    from portal_mcp.config import default_paths
    from portal_mcp.manifest import load_manifest_file

    path = args.manifest or (default_paths().services / f"{args.service}.yaml")
    manifest = load_manifest_file(path)
    return (urlsplit(manifest.base_url).hostname or "").lower(), list(manifest.data_hosts), list(manifest.login_hosts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a candidate tool catalog from a HAR file.")
    parser.add_argument("har", type=Path)
    parser.add_argument("--manifest", type=Path, help="manifest file; its base_url, data_hosts and login_hosts are used")
    parser.add_argument("--service", help="service name, to find services/<name>.yaml")
    parser.add_argument("--host", help="the service host, for example app.example.com (without a manifest)")
    parser.add_argument("--writes", type=Path, help="captured_writes.json from record.py")
    parser.add_argument("--data-host", action="append", default=[],
                        help="a declared data host whose reads to include (repeatable; without a manifest)")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="analyze a recording whose login was not verified; the catalog is marked logged_in false")
    parser.add_argument("--keep-har", action="store_true",
                        help="keep the HAR file; by default it is deleted once every host is analyzed")
    parser.add_argument(
        "--show-values-for",
        default="",
        help="comma-separated field names whose captured values may be shown (for fixed fields only)",
    )
    args = parser.parse_args(argv)
    from_manifest = _hosts_from_manifest(args)
    if from_manifest is None and not args.host:
        parser.error("give --manifest, --service or --host")
    logged_in = _login_state(args.har)
    if logged_in is False and not args.allow_unverified:
        print(
            f"[portal-mcp] The login was not verified in this recording, so it only shows the logged-out "
            f"state. Record again, or pass --allow-unverified. Kept {args.har}.",
            file=sys.stderr,
        )
        return 2
    if logged_in is None:
        print("[portal-mcp] No recording.json next to the HAR; cannot tell whether the login was verified.",
              file=sys.stderr)
    har = json.loads(args.har.read_text(encoding="utf-8"))

    failed: list[str] = []
    if from_manifest is not None:
        service_host, data_hosts, login_hosts = from_manifest
        catalog = analyze_hosts(har, service_host, data_hosts, login_hosts)
        failed = catalog["failed_hosts"]
    else:
        service_host = args.host
        catalog = analyze(har, args.host, args.data_host)
    if args.writes:
        captured = json.loads(args.writes.read_text(encoding="utf-8"))
        shown = {n.strip() for n in args.show_values_for.split(",") if n.strip()}
        catalog["writes"] = analyze_writes(captured, service_host, shown)
    catalog["logged_in"] = logged_in
    json.dump(catalog, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()

    # The HAR holds cookies and page content. Delete it only when every host was analyzed.
    if failed:
        print(f"[portal-mcp] Analysis failed for {', '.join(failed)}; kept {args.har} so it can be analyzed again.",
              file=sys.stderr)
        return 1
    if not args.keep_har:
        args.har.unlink(missing_ok=True)
        print(f"[portal-mcp] Deleted {args.har} (it held cookies and page content).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
