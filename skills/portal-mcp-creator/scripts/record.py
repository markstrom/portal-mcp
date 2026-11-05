"""Guided recording: log in by hand, verify the login, then visit chosen pages while every
request is recorded.

Order of a run:
1. The session check is called without cookies (preflight). A check that answers 200 even
   when logged out cannot verify a login, so the run stops. A redirect is a warning.
2. A clean, temporary browser profile is created for this run and deleted afterwards
   (--reuse-profile uses the persistent profile instead, only when the user chose it).
3. The user logs in. Only a 200 from the session check counts as logged in.
4. With --discover: after the login is verified, the menu links of the page are written to
   menu_links.json, and the run waits for visit.json (the user's choice) before visiting.
5. The chosen pages are visited. After login, the guard blocks non-GET requests (captured,
   not sent), GET requests that look like side effects, login hosts, and every other host.

The HAR file and captured_writes.json contain cookies, response bodies and form values.
analyze.py deletes the HAR when it is done; delete the recording directory afterwards.

Usage:
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python record.py --service example --discover --out <dir>
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python record.py --service example \\
      --visit /invoices --visit /reports --out <recording dir>
  # forms by hand (writes captured, not sent), until the user closes the window:
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python record.py --service example --interactive
  # only after the user explicitly agreed to a real action:
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python record.py --service example --interactive --allow-real-writes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guard import decide, should_capture  # noqa: E402
from links import COLLECT_JS, filter_menu_links, valid_visit  # noqa: E402

from portal_mcp.config import default_paths  # noqa: E402
from portal_mcp.diagnostics import (  # noqa: E402
    cookie_summary,
    header_too_large_message,
    is_header_too_large,
    is_tracking_host,
)
from portal_mcp.login.base import LoginError  # noqa: E402
from portal_mcp.login.local import (  # noqa: E402
    LoginObservation,
    _capture,
    _first_page,
    _launch,
    _user_agent,
    candidate_verdict,
    check_status,
    login_not_verified_message,
    manifest_with_check,
    preflight_check,
)
from portal_mcp.manifest import Manifest, load_manifest_file  # noqa: E402

MAX_CAPTURED_BODY = 64 * 1024
MAX_BLOCKED_OTHER = 200


def status(message: str) -> None:
    """One status line on stderr; stdout is reserved for the JSON summary."""
    print(f"[portal-mcp] {message}", file=sys.stderr, flush=True)


def _write_private(path: Path, data) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def profile_for_run(paths, manifest: Manifest, profile: str, out_dir: Path, reuse_profile: bool) -> tuple[Path, bool]:
    """(profile directory, temporary?). Clean and temporary unless the user chose to reuse."""
    if reuse_profile:
        return paths.profiles / manifest.name / profile, False
    return out_dir / "browser-profile", True


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
    reuse_profile: bool = False,
    discover: bool = False,
    choice_timeout: float = 900,
    poll_seconds: float = 2.0,
    check_candidates: list[str] | tuple[str, ...] = (),
) -> dict:
    from playwright.async_api import async_playwright

    paths = default_paths()
    paths.ensure()
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    har_path = out_dir / "recording.har"
    captured_path = out_dir / "captured_writes.json"
    service_host = (urlsplit(manifest.base_url).hostname or "").lower()
    login_hosts = tuple(manifest.login_hosts)
    headless = os.environ.get("PORTAL_MCP_HEADLESS") == "1"

    profile_dir, temporary_profile = profile_for_run(paths, manifest, profile, out_dir, reuse_profile)
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)

    state = {"logged_in": False, "header_too_large": None}
    blocked_other: list[dict] = []
    blocked_tracking: dict[str, int] = {}
    captured: list[dict] = []
    undeclared_hosts: dict[str, int] = {}
    response_tasks: list[asyncio.Task] = []
    summary: dict = {
        "service": manifest.name,
        "profile": {"clean": temporary_profile, "reused": not temporary_profile},
        "login_hosts": list(login_hosts),
        "session_check": None,
        "login_verified": False,
        "menu_links": None,
        "visited": [],
        "blocked_tracking": blocked_tracking,
        "blocked_other": blocked_other,
        "blocked_navigation_hosts": undeclared_hosts,
        "header_too_large": None,
        "check_candidates": [],
        "captured_writes": 0,
        "real_writes_allowed": allow_real_writes,
        "error": None,
    }

    # 1. Preflight: the check without cookies must not answer 200.
    preflight = await preflight_check(manifest)
    summary["session_check"] = {"logged_out_answer": preflight.status, "verdict": preflight.verdict,
                                "note": preflight.explain(manifest)}
    if preflight.verdict == "open":
        summary["error"] = preflight.explain(manifest)
        status(summary["error"])
        return summary

    # Candidate session checks: their logged-out answer, before the browser opens.
    candidates: list[dict] = []
    for candidate in check_candidates:
        path = valid_visit(candidate, service_host)
        if path is None:
            candidates.append({"path": str(candidate)[:200], "refused": "must be a relative path that is not a side effect"})
            continue
        result = await preflight_check(manifest_with_check(manifest, path))
        candidates.append({"path": path, "logged_out": result, "logged_in": None})
        status(f"Candidate session check {path}: {result.status or result.error} when logged out.")
    if preflight.verdict == "redirects":
        status("Warning: " + preflight.explain(manifest))
    status(f"Using a {'clean, temporary' if temporary_profile else 'reused'} browser profile.")

    try:
        async with async_playwright() as playwright:
            context = await _launch(
                playwright, profile_dir, headless, status=status,
                record_har_path=str(har_path), record_har_content="embed",
            )
            try:
                async def route_handler(route, request):
                    decision = decide(
                        request.method, request.url, service_host, state["logged_in"],
                        allow_writes=allow_real_writes, login_hosts=login_hosts,
                    )
                    if decision.allowed:
                        await route.continue_()
                        return
                    parts = urlsplit(request.url)
                    host = (parts.hostname or "").lower()
                    if host and host != service_host and is_tracking_host(host):
                        blocked_tracking[host] = blocked_tracking.get(host, 0) + 1
                    elif len(blocked_other) < MAX_BLOCKED_OTHER:
                        blocked_other.append({"method": request.method, "host": host, "path": parts.path,
                                              "reason": decision.reason})
                    if request.is_navigation_request() and host != service_host and host not in login_hosts:
                        undeclared_hosts[host] = undeclared_hosts.get(host, 0) + 1
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

                async def inspect_response(response):
                    if state["header_too_large"] is not None:
                        return
                    if (urlsplit(response.url).hostname or "").lower() != service_host:
                        return
                    if response.status not in (400, 413, 431, 494):
                        return
                    try:
                        body = await response.text()
                    except Exception:
                        body = ""
                    if is_header_too_large(response.status, body[:4000], response.status_text):
                        cookies = await context.cookies([manifest.base_url])
                        state["header_too_large"] = {
                            "status": response.status,
                            "host": service_host,
                            "cookies": cookie_summary(cookies),
                            "message": header_too_large_message(manifest.name, service_host, cookies),
                        }
                        status(state["header_too_large"]["message"])

                await context.route("**/*", route_handler)
                context.on("response", lambda r: response_tasks.append(asyncio.ensure_future(inspect_response(r))))

                page = await _first_page(context)
                status(f"Browser window open. Log in to {manifest.name} in the window.")
                await page.goto(manifest.login_url)

                # 3. Wait for a verified login.
                observation = LoginObservation()
                deadline = time.monotonic() + timeout
                while True:
                    if state["header_too_large"] is not None:
                        raise LoginError(state["header_too_large"]["message"])
                    if await _capture(context, page, manifest, observation=observation) is not None:
                        break
                    if time.monotonic() > deadline:
                        raise LoginError(login_not_verified_message(manifest, observation, timeout))
                    await asyncio.sleep(poll_seconds)
                state["logged_in"] = True
                summary["login_verified"] = True
                summary["session_check"]["logged_in_answer"] = 200
                status("Login verified.")

                # Candidate session checks: their logged-in answer, with a copy of the cookies.
                if candidates:
                    cookies = await context.cookies([manifest.base_url])
                    agent = await _user_agent(page)
                    for entry in candidates:
                        if "refused" in entry:
                            continue
                        entry["logged_in"] = await check_status(
                            manifest_with_check(manifest, entry["path"]), cookies, user_agent=agent
                        )
                        status(f"Candidate session check {entry['path']}: "
                               f"{entry['logged_in'].status or entry['logged_in'].error} when logged in.")

                # 4. Menu links, only now.
                chosen: list[str] = []
                if discover:
                    current = urlsplit(page.url)
                    on_login_page = current.path == (urlsplit(manifest.login_url).path or "/")
                    if (current.hostname or "").lower() != service_host or on_login_page:
                        # Back to the service's start page (same host, GET) to find its menu.
                        await page.goto(manifest.base_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    raw = await page.evaluate(COLLECT_JS)
                    found = filter_menu_links(raw, service_host, logged_in=state["logged_in"])
                    _write_private(out_dir / "menu_links.json", found)
                    summary["menu_links"] = {"count": len(found["links"]), "dropped": found["dropped"],
                                             "file": str(out_dir / "menu_links.json")}
                    status(f"Found {len(found['links'])} menu links. Waiting for the user's choice in "
                           f"{out_dir / 'visit.json'} (a JSON list of paths; [] to visit none).")
                    choice_file = out_dir / "visit.json"
                    waited = time.monotonic() + choice_timeout
                    while not choice_file.exists():
                        if time.monotonic() > waited:
                            status("No choice arrived in time; visiting no pages.")
                            break
                        await asyncio.sleep(1)
                    if choice_file.exists():
                        try:
                            chosen = json.loads(choice_file.read_text(encoding="utf-8")) or []
                        except ValueError:
                            chosen = []

                # 5. Visits.
                for visit in [*visits, *chosen]:
                    path = valid_visit(visit, service_host)
                    if path is None:
                        summary["visited"].append({"path": str(visit)[:200], "status": "refused"})
                        continue
                    url = urljoin(manifest.base_url.rstrip("/") + "/", path.lstrip("/"))
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=30000)
                        summary["visited"].append({"path": path, "status": "ok"})
                    except Exception as exc:  # keep going with the other pages
                        summary["visited"].append({"path": path, "status": f"error: {type(exc).__name__}"})

                if interactive:
                    status(
                        "Interactive mode: fill in and submit the forms in the browser window. "
                        + ("Writes are sent for real. " if allow_real_writes else "Writes are captured and not sent. ")
                        + "Close the window when you are done."
                    )
                    try:
                        await context.wait_for_event("close", timeout=interactive_timeout * 1000)
                    except Exception:
                        pass
            finally:
                for task in response_tasks:
                    task.cancel()
                await asyncio.gather(*response_tasks, return_exceptions=True)
                try:
                    await context.close()  # writes the HAR file
                except Exception:
                    pass
    except LoginError as exc:
        summary["error"] = str(exc)
        status(str(exc))
    finally:
        summary["header_too_large"] = state["header_too_large"]
        if temporary_profile:
            shutil.rmtree(profile_dir, ignore_errors=True)

    summary["check_candidates"] = [_candidate_report(entry) for entry in candidates]
    _write_private(captured_path, captured)
    summary["captured_writes"] = len(captured)
    summary["har"] = str(har_path) if har_path.exists() else None
    summary["captured_file"] = str(captured_path)
    return summary


def _answer(result) -> dict | None:
    if result is None:
        return None
    return {
        "status": result.status,
        "redirect_host": result.location_host,
        "content_type": (result.content_type or "").split(";")[0] or None,
        "error": result.error,
    }


def _candidate_report(entry: dict) -> dict:
    if "refused" in entry:
        return {"path": entry["path"], "usable": False, "reason": entry["refused"]}
    usable, reason = candidate_verdict(entry["logged_out"], entry["logged_in"])
    return {
        "path": entry["path"],
        "logged_out": _answer(entry["logged_out"]),
        "logged_in": _answer(entry["logged_in"]),
        "usable": usable,
        "reason": reason,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record a guided session for one service.")
    parser.add_argument("--manifest", type=Path, help="manifest file; defaults to services/<service>.yaml")
    parser.add_argument("--service", help="service name, used to find the manifest")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--visit", action="append", default=[], help="path to visit after login (repeatable)")
    parser.add_argument("--out", type=Path, help="output directory; defaults to a new one under recordings/")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait for the login")
    parser.add_argument("--discover", action="store_true",
                        help="after a verified login, list menu links and wait for visit.json")
    parser.add_argument("--choice-timeout", type=float, default=900)
    parser.add_argument("--check-candidate", action="append", default=[],
                        help="an endpoint to try as session.check, logged out and logged in (repeatable)")
    parser.add_argument("--reuse-profile", action="store_true",
                        help="use the persistent browser profile instead of a clean one (only if the user chose it)")
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
        reuse_profile=args.reuse_profile,
        discover=args.discover,
        choice_timeout=args.choice_timeout,
        check_candidates=args.check_candidate,
    ))
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    if summary.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
