"""Reconnaissance without logging in: public API docs, endpoints found in the web app's JavaScript,
whether the site has public content and a login, and what robots.txt says.

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
from urllib.robotparser import RobotFileParser

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


ANCHOR = re.compile(r"<a\b[^>]*href=[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
TAG = re.compile(r"<[^>]+>")
PASSWORD_INPUT = re.compile(r"<input\b[^>]*type=[\"']?password", re.IGNORECASE)
# Words of sign-in links and paths, in English and Swedish.
LOGIN_TEXT = re.compile(
    r"\b(log ?in|sign ?in|logga in|mitt konto|my account|mina sidor|my pages|bankid)\b", re.IGNORECASE
)
LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|logga-in|account/login|auth|oauth|connect/authorize)\b",
                        re.IGNORECASE)
MAX_DISALLOWED = 50


def page_profile(html: str, status: int, base_url: str) -> dict:
    """Does the start page show public content, and does it offer a login? Structure only."""
    host = (urlsplit(base_url).hostname or "").lower()
    same_host_links = 0
    login_links: list[dict] = []
    for href, inner in ANCHOR.findall(html):
        url = urljoin(base_url.rstrip("/") + "/", href)
        text = TAG.sub(" ", inner)
        text = " ".join(text.split())[:60]
        if (urlsplit(url).hostname or "").lower() == host:
            same_host_links += 1
        if LOGIN_TEXT.search(text) or LOGIN_PATH.search(urlsplit(url).path):
            if len(login_links) < 10:
                login_links.append({"host": urlsplit(url).hostname, "path": urlsplit(url).path, "text": text})
    password_form = bool(PASSWORD_INPUT.search(html))
    return {
        "status": status,
        "public_content": status == 200 and same_host_links >= 5 and not password_form,
        "same_host_links": same_host_links,
        "login_found": bool(login_links) or password_form,
        "login_links": login_links,
        "password_form_on_start_page": password_form,
    }


def robots_rules(text: str, base_url: str) -> dict:
    """What robots.txt allows for any crawler: the start page, and the disallowed paths."""
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    disallowed: list[str] = []
    applies = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if key.lower() == "user-agent":
            applies = value == "*"
        elif key.lower() == "disallow" and applies and value and len(disallowed) < MAX_DISALLOWED:
            disallowed.append(value)
    return {
        "found": True,
        "start_page_allowed": parser.can_fetch("*", base_url.rstrip("/") + "/"),
        "disallowed_for_all": disallowed,
    }


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
    result["site"] = page_profile(page.text if page.status_code == 200 else "", page.status_code, base)
    robots = client.get(base + "/robots.txt")
    result["robots"] = (robots_rules(robots.text, base) if robots.status_code == 200
                        else {"found": False, "status": robots.status_code})
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
