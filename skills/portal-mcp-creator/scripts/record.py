"""Guided recording: log in by hand, then visit chosen pages while every request is recorded.

After login, the guard blocks non-GET requests and GET requests that look like side effects,
and blocks everything outside the service's own host. Blocked non-GET requests to the
service's own host are captured (method, path, content type, body) and aborted before they
leave the browser, so the format of a write becomes known without performing it.

The HAR file and captured_writes.json contain cookies, response bodies and form values.
Delete the recording directory once the manifest is written.

Usage:
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python record.py --service example \\
      --visit /invoices --visit /reports --out <recording dir>

  # Let the user fill in and submit forms by hand; writes are captured, not sent.
  # The recording ends when the user closes the browser window.
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python record.py --service example --interactive

  # Only after the user explicitly agreed: let writes through for real (own host only).
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python record.py --service example --interactive --allow-real-writes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guard import decide, should_capture  # noqa: E402

from portal_mcp.config import default_paths  # noqa: E402
from portal_mcp.login.base import LoginError  # noqa: E402
from portal_mcp.login.local import _capture, _launch  # noqa: E402
from portal_mcp.manifest import Manifest, load_manifest_file  # noqa: E402

MAX_CAPTURED_BODY = 64 * 1024


async def record(
    manifest: Manifest,
    visits: list[str],
    out_dir: Path,
    timeout: float,
    profile: str,
    *,
    interactive: bool = False,
    interactive_timeout: float = 1800,
    allow_real_writes: bool = False,
) -> dict:
    from playwright.async_api import async_playwright

    paths = default_paths()
    paths.ensure()
    profile_dir = paths.profiles / manifest.name / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    har_path = out_dir / "recording.har"
    captured_path = out_dir / "captured_writes.json"
    service_host = urlsplit(manifest.base_url).hostname or ""
    headless = os.environ.get("PORTAL_MCP_HEADLESS") == "1"

    login_hosts = tuple(manifest.login_hosts)
    state = {"logged_in": False}
    blocked: list[dict] = []
    captured: list[dict] = []
    undeclared_hosts: dict[str, int] = {}
    summary: dict = {
        "service": manifest.name,
        "login_hosts": list(login_hosts),
        "visited": [],
        "blocked": blocked,
        "blocked_navigation_hosts": undeclared_hosts,
        "captured_writes": 0,
        "real_writes_allowed": allow_real_writes,
        "logged_in": False,
    }

    async with async_playwright() as playwright:
        context = await _launch(
            playwright, profile_dir, headless, record_har_path=str(har_path), record_har_content="embed"
        )
        try:
            async def route_handler(route, request):
                decision = decide(
                    request.method,
                    request.url,
                    service_host,
                    state["logged_in"],
                    allow_writes=allow_real_writes,
                    login_hosts=login_hosts,
                )
                if decision.allowed:
                    await route.continue_()
                    return
                parts = urlsplit(request.url)
                host = (parts.hostname or "").lower()
                blocked.append({"method": request.method, "host": host, "path": parts.path, "reason": decision.reason})
                # A page that navigates to an undeclared host (for example a login redirect) is
                # reported by host name, so the user can decide whether to declare it. Never automatic.
                if request.is_navigation_request() and host != service_host and host not in login_hosts:
                    undeclared_hosts[host] = undeclared_hosts.get(host, 0) + 1
                # Writes are captured only for the service's own host, never for login hosts.
                if should_capture(request.method, request.url, service_host, state["logged_in"]):
                    body = request.post_data or ""
                    captured.append({
                        "method": request.method.upper(),
                        "path": parts.path,
                        "query": parts.query,
                        "content_type": (await request.all_headers()).get("content-type", ""),
                        "body": body[:MAX_CAPTURED_BODY],
                        "page": urlsplit(request.frame.url).path if request.frame else None,
                    })
                await route.abort("blockedbyclient")

            await context.route("**/*", route_handler)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(manifest.login_url)

            deadline = time.monotonic() + timeout
            while True:
                if await _capture(context, page, manifest) is not None:
                    break
                if time.monotonic() > deadline:
                    raise LoginError(f"login to {manifest.name} timed out")
                await asyncio.sleep(2)
            state["logged_in"] = True
            summary["logged_in"] = True

            for visit in visits:
                url = urljoin(manifest.base_url.rstrip("/") + "/", visit.lstrip("/"))
                try:
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    summary["visited"].append({"path": urlsplit(url).path, "status": "ok"})
                except Exception as exc:  # keep going with the other pages
                    summary["visited"].append({"path": urlsplit(url).path, "status": f"error: {type(exc).__name__}"})

            if interactive:
                print(
                    "Interactive mode: fill in and submit the forms in the browser window. "
                    + ("Writes are sent for real. " if allow_real_writes else "Writes are captured and not sent. ")
                    + "Close the window when you are done.",
                    file=sys.stderr,
                )
                try:
                    await context.wait_for_event("close", timeout=interactive_timeout * 1000)
                except Exception:
                    pass  # timeout: close it ourselves below
        finally:
            try:
                await context.close()  # writes the HAR file
            except Exception:
                pass

    fd = os.open(captured_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(captured, handle, indent=2, ensure_ascii=False)
    summary["captured_writes"] = len(captured)
    summary["har"] = str(har_path)
    summary["captured_file"] = str(captured_path)
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record a guided session for one service.")
    parser.add_argument("--manifest", type=Path, help="manifest file; defaults to services/<service>.yaml")
    parser.add_argument("--service", help="service name, used to find the manifest")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--visit", action="append", default=[], help="path to visit after login (repeatable)")
    parser.add_argument("--out", type=Path, help="output directory; defaults to a new one under recordings/")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait for the login")
    parser.add_argument("--interactive", action="store_true", help="keep the window open until the user closes it")
    parser.add_argument("--interactive-timeout", type=float, default=1800)
    parser.add_argument(
        "--allow-real-writes",
        action="store_true",
        help="send writes for real (own host only). Only after the user explicitly agreed.",
    )
    args = parser.parse_args(argv)

    manifest_path = args.manifest or (default_paths().services / f"{args.service}.yaml")
    manifest = load_manifest_file(manifest_path)
    out_dir = args.out or (default_paths().recordings / f"{manifest.name}-{datetime.now():%Y%m%d-%H%M%S}")
    summary = asyncio.run(record(
        manifest, args.visit, out_dir, args.timeout, args.profile,
        interactive=args.interactive,
        interactive_timeout=args.interactive_timeout,
        allow_real_writes=args.allow_real_writes,
    ))
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
