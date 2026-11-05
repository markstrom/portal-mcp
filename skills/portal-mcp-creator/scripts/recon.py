"""Reconnaissance without logging in: public API docs and endpoints found in the web app's JavaScript.

Usage:
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python recon.py https://app.example.com > recon.json

Only the service's own host is contacted, and only GET requests are made.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import urljoin, urlsplit

import httpx

DOC_PATHS = [
    "/openapi.json", "/swagger.json", "/v3/api-docs", "/v2/api-docs", "/api-docs",
    "/swagger", "/swagger/index.html", "/api/docs", "/docs/api",
]
MAX_SCRIPTS = 25
MAX_SCRIPT_BYTES = 5 * 1024 * 1024

SCRIPT_SRC = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
# String literals that look like API paths, e.g. "/api/v2/invoices"
PATH_LITERAL = re.compile(r"[\"'`](/(?:api|rest|graphql|v\d+)[A-Za-z0-9_\-./{}$:?=&]*)[\"'`]")
# Calls like fetch("...", {method:"POST"}) or axios.post("...")
CALL = re.compile(
    r"(?:fetch|axios\.(get|post|put|patch|delete)|\.(get|post|put|patch|delete))\(\s*[\"'`]([^\"'`]+)[\"'`]"
    r"(?:\s*,\s*\{[^}]{0,300}?method\s*:\s*[\"'](GET|POST|PUT|PATCH|DELETE)[\"'])?",
    re.IGNORECASE,
)


def extract_endpoints(source: str) -> list[dict]:
    found: dict[tuple[str, str], dict] = {}
    for match in CALL.finditer(source):
        verb = (match.group(1) or match.group(2) or match.group(4) or "GET").upper()
        target = match.group(3)
        if not target.startswith("/") and "://" not in target:
            continue
        found[(verb, target)] = {"method": verb, "path": target, "source": "call"}
    for match in PATH_LITERAL.finditer(source):
        target = match.group(1)
        found.setdefault(("?", target), {"method": "?", "path": target, "source": "literal"})
    return sorted(found.values(), key=lambda e: (e["path"], e["method"]))


def recon(base_url: str, client: httpx.Client) -> dict:
    base = base_url.rstrip("/")
    host = urlsplit(base).hostname
    result: dict = {"base_url": base, "docs": [], "scripts": [], "endpoints": []}

    for path in DOC_PATHS:
        response = client.get(base + path)
        if response.status_code == 200 and response.text.strip():
            entry = {"path": path, "content_type": response.headers.get("content-type", "")}
            if "json" in entry["content_type"]:
                try:
                    spec = response.json()
                    entry["openapi"] = spec.get("openapi") or spec.get("swagger")
                    entry["paths"] = sorted((spec.get("paths") or {}).keys())[:500]
                except ValueError:
                    pass
            result["docs"].append(entry)

    page = client.get(base + "/")
    scripts = SCRIPT_SRC.findall(page.text) if page.status_code == 200 else []
    endpoints: dict[tuple[str, str], dict] = {}
    for src in scripts[:MAX_SCRIPTS]:
        url = urljoin(base + "/", src)
        if (urlsplit(url).hostname or "") != host:
            result["scripts"].append({"url": url, "skipped": "other host"})
            continue
        response = client.get(url)
        if response.status_code != 200 or len(response.content) > MAX_SCRIPT_BYTES:
            result["scripts"].append({"url": url, "skipped": f"status {response.status_code} or too large"})
            continue
        result["scripts"].append({"url": url, "bytes": len(response.content)})
        for endpoint in extract_endpoints(response.text):
            endpoints.setdefault((endpoint["method"], endpoint["path"]), endpoint)
    result["endpoints"] = list(endpoints.values())
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Find public API docs and JavaScript endpoints.")
    parser.add_argument("url", help="base URL of the web app, for example https://app.example.com")
    args = parser.parse_args(argv)
    with httpx.Client(follow_redirects=True, timeout=20, headers={"user-agent": "portal-mcp-recon"}) as client:
        json.dump(recon(args.url, client), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
