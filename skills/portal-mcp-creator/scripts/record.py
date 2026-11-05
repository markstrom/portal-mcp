"""Guided recording: log in by hand, then visit chosen pages while every request is recorded.

After login, the guard blocks non-GET requests and GET requests that look like side effects,
and blocks everything outside the service's own host. The HAR file contains cookies and
response bodies. Delete the recording directory once the manifest is written.

Usage:
  uv run --project "$CLAUDE_PLUGIN_ROOT" python record.py --service example \
      --visit /invoices --visit /reports --out <recording dir>
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

from guard import decide  # noqa: E402

from portal_mcp.config import default_paths  # noqa: E402
from portal_mcp.login.base import LoginError  # noqa: E402
from portal_mcp.login.local import _capture, _launch  # noqa: E402
from portal_mcp.manifest import Manifest, load_manifest_file  # noqa: E402


async def record(manifest: Manifest, visits: list[str], out_dir: Path, timeout: float, profile: str) -> dict:
    from playwright.async_api import async_playwright

    paths = default_paths()
    paths.ensure()
    profile_dir = paths.profiles / manifest.name / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    har_path = out_dir / "recording.har"
    service_host = urlsplit(manifest.base_url).hostname or ""
    headless = os.environ.get("PORTAL_MCP_HEADLESS") == "1"

    state = {"logged_in": False}
    blocked: list[dict] = []
    summary: dict = {"service": manifest.name, "visited": [], "blocked": blocked, "logged_in": False}

    async with async_playwright() as playwright:
        context = await _launch(
            playwright, profile_dir, headless, record_har_path=str(har_path), record_har_content="embed"
        )
        try:
            async def route_handler(route, request):
                decision = decide(request.method, request.url, service_host, state["logged_in"])
                if decision.allowed:
                    await route.continue_()
                    return
                blocked.append({"method": request.method, "path": urlsplit(request.url).path, "reason": decision.reason})
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
        finally:
            await context.close()  # writes the HAR file

    summary["har"] = str(har_path)
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record a guided session for one service.")
    parser.add_argument("--manifest", type=Path, help="manifest file; defaults to services/<service>.yaml")
    parser.add_argument("--service", help="service name, used to find the manifest")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--visit", action="append", default=[], help="path to visit after login (repeatable)")
    parser.add_argument("--out", type=Path, help="output directory; defaults to a new one under recordings/")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait for the login")
    args = parser.parse_args(argv)

    manifest_path = args.manifest or (default_paths().services / f"{args.service}.yaml")
    manifest = load_manifest_file(manifest_path)
    out_dir = args.out or (default_paths().recordings / f"{manifest.name}-{datetime.now():%Y%m%d-%H%M%S}")
    summary = asyncio.run(record(manifest, args.visit, out_dir, args.timeout, args.profile))
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
