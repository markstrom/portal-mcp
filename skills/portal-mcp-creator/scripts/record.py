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
6. With --explore: on each visited page the recorder clicks safe controls itself (tabs,
   expandable sections, "show more"), so data that loads only after a click is recorded.
   Controls that mean an action are never clicked; the guard still blocks every write.

The HAR file and captured_writes.json contain cookies, response bodies and form values.
analyze.py deletes the HAR when it is done; delete the recording directory afterwards.

Usage:
  uv run --project "${CLAUDE_PLUGIN_ROOT}" python record.py --service example --discover --explore --out <dir>
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

from guard import DISCOVERY_PATHS, decide, oidc_endpoints_from, should_capture  # noqa: E402
from check_suggest import candidate_from, rank  # noqa: E402
from explore import (  # noqa: E402
    COLLECT_CONTROLS_JS, COLLECT_SEARCH_JS, MAX_CLICKS_PER_PAGE, control_key, decline_control, pick_controls,
    search_fields,
)  # noqa: E402
from links import COLLECT_JS, filter_menu_links, valid_visit  # noqa: E402

from portal_mcp.config import default_paths  # noqa: E402
from portal_mcp.diagnostics import (  # noqa: E402
    cookie_summary,
    header_too_large_message,
    is_consent_host,
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
# Storage keys that look like an app's token. Only names and the shape of the value are
# reported, never the value.
TOKEN_KEYS_JS = r"""() => {
  const out = [];
  const looks = /token|oidc|auth|msal|access|jwt|session/i;
  for (const kind of ['sessionStorage', 'localStorage']) {
    const store = window[kind];
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (!looks.test(key)) continue;
      const value = store.getItem(key) || '';
      let fields = [];
      try { const parsed = JSON.parse(value); if (parsed && typeof parsed === 'object') fields = Object.keys(parsed).slice(0, 30); } catch (e) {}
      out.push({source: kind + ':' + key, json_fields: fields, looks_like_jwt: /^ey[A-Za-z0-9_-]+\./.test(value)});
    }
  }
  return out;
}"""
# Header names that carry credentials. Only their names are ever reported, never values.
AUTH_HEADER_NAMES = ("x-api-key", "api-key", "apikey", "x-auth-token", "x-access-token", "x-xsrf-token",
                     "x-csrf-token", "ocp-apim-subscription-key", "x-functions-key", "x-session-token")


def observe_auth(stats: dict, headers: dict[str, str]) -> None:
    """Count how requests to a data host authenticate: names and schemes only, never values."""
    stats["requests"] = stats.get("requests", 0) + 1
    authorization = headers.get("authorization")
    if authorization:
        scheme = authorization.split(" ", 1)[0][:20]
        schemes = stats.setdefault("authorization_schemes", {})
        schemes[scheme] = schemes.get(scheme, 0) + 1
    if headers.get("cookie"):
        stats["with_cookies"] = stats.get("with_cookies", 0) + 1
    names = sorted({name for name in headers if name.lower() in AUTH_HEADER_NAMES})
    if names:
        known = set(stats.get("auth_headers", []))
        stats["auth_headers"] = sorted(known | set(names))
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
    suggest_check: bool = False,
    explore: bool = False,
    click_wait: float = 5.0,
    search_text: str = "test",
    depth: int = 1,
    max_pages: int = 100,
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
    data_hosts = tuple(manifest.data_hosts)
    asset_hosts = tuple(manifest.asset_hosts)
    data_host_auth: dict[str, dict] = {}
    asset_hosts_used: dict[str, int] = {}
    consent_hosts_used: dict[str, int] = {}
    oidc_endpoints: dict[str, dict[str, set[str]]] = {}
    oidc_after_login: dict[str, int] = {}
    headless = os.environ.get("PORTAL_MCP_HEADLESS") == "1"
    # A public site (session.type none): no login, the pages are visited directly. The guard
    # still treats the run as logged in, so every write is blocked and captured.
    public = not manifest.needs_login
    # Traffic counters, read before and after each exploring click.
    traffic = {"requests": 0, "json_reads": 0, "data_host": 0, "captured": 0, "downloads": 0}
    explored: list[dict] = []

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
        "data_hosts": list(data_hosts),
        "data_host_auth": data_host_auth,
        "asset_hosts": list(asset_hosts),
        "asset_hosts_used": asset_hosts_used,
        "consent_hosts_used": consent_hosts_used,
        "login_hosts_after_login": manifest.login_hosts_after_login,
        "oidc_after_login": oidc_after_login,
        "session_check": None,
        "login_verified": False,
        "public": public,
        "menu_links": None,
        "visited": [],
        "explored": None,
        "token_storage": None,
        "blocked_tracking": blocked_tracking,
        "blocked_other": blocked_other,
        "blocked_navigation_hosts": undeclared_hosts,
        "header_too_large": None,
        "check_candidates": [],
        "suggested_session_checks": None,
        "captured_writes": 0,
        "real_writes_allowed": allow_real_writes,
        "error": None,
    }

    # 1. Preflight: the check without cookies must not answer 200.
    if public:
        check_candidates = ()
        status("Public site (session.type none): no login, the pages are visited directly.")
    preflight = await preflight_check(manifest) if not public else None
    if preflight is not None:
        summary["session_check"] = {"logged_out_answer": preflight.status, "verdict": preflight.verdict,
                                    "note": preflight.explain(manifest)}
    if preflight is not None and preflight.verdict == "open":
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
    if preflight is not None and preflight.verdict == "redirects":
        status("Warning: " + preflight.explain(manifest))
    # Look for a better check unless the current one already answers 401/403 when logged out.
    suggest = not public and (suggest_check or preflight.verdict != "rejects")
    seen_json: list[str] = []
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
                        allow_writes=allow_real_writes, login_hosts=login_hosts, data_hosts=data_hosts,
                        asset_hosts=asset_hosts, resource_type=request.resource_type,
                        login_after_login=manifest.login_hosts_after_login, oidc_endpoints=oidc_endpoints,
                    )
                    parts = urlsplit(request.url)
                    host = (parts.hostname or "").lower()
                    if decision.allowed:
                        traffic["requests"] += 1
                        if host in data_hosts:
                            traffic["data_host"] += 1
                        if host in data_hosts and state["logged_in"]:
                            observe_auth(data_host_auth.setdefault(host, {}), await request.all_headers())
                        if host in asset_hosts:
                            asset_hosts_used[host] = asset_hosts_used.get(host, 0) + 1
                        if is_consent_host(host):
                            consent_hosts_used[host] = consent_hosts_used.get(host, 0) + 1
                        if host in login_hosts and state["logged_in"]:
                            key = f"{request.method.upper()} {parts.path}"
                            oidc_after_login[key] = oidc_after_login.get(key, 0) + 1
                        if (manifest.login_hosts_after_login == "oidc" and host in login_hosts
                                and request.method.upper() == "GET" and parts.path == DISCOVERY_PATHS[0]):
                            # Learn the endpoints from the discovery document as it passes, so
                            # only those named in it are opened after login.
                            response = await route.fetch()
                            try:
                                oidc_endpoints[host] = oidc_endpoints_from(json.loads(await response.text()), host)
                            except ValueError:
                                pass
                            await route.fulfill(response=response)
                            return
                        await route.continue_()
                        return
                    if host and host != service_host and is_tracking_host(host):
                        blocked_tracking[host] = blocked_tracking.get(host, 0) + 1
                    elif len(blocked_other) < MAX_BLOCKED_OTHER:
                        blocked_other.append({"method": request.method, "host": host, "path": parts.path,
                                              "reason": decision.reason})
                    if (request.is_navigation_request() and host != service_host
                            and host not in login_hosts and host not in data_hosts):
                        undeclared_hosts[host] = undeclared_hosts.get(host, 0) + 1
                    if should_capture(request.method, request.url, service_host, state["logged_in"], data_hosts):
                        body = request.post_data or ""
                        traffic["captured"] += 1
                        captured.append({
                            "host": host,
                            "method": request.method.upper(),
                            "path": parts.path,
                            "query": parts.query,
                            "content_type": (await request.all_headers()).get("content-type", ""),
                            "body": body[:MAX_CAPTURED_BODY],
                            "page": urlsplit(request.frame.url).path if request.frame else None,
                        })
                    await route.abort("blockedbyclient")

                async def inspect_response(response):
                    if (response.request.method.upper() == "GET" and response.status == 200
                            and "json" in (response.headers.get("content-type") or "")):
                        traffic["json_reads"] += 1
                    if suggest and state["logged_in"]:
                        found = candidate_from(
                            response.request.method, response.url, response.status,
                            response.headers.get("content-type"), service_host, manifest.session.check,
                        )
                        if found is not None:
                            seen_json.append(found)
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
                if explore:
                    # A click must never leave a dialog or a second window behind.
                    context.on("page", lambda p: asyncio.ensure_future(_close_extra(context, p)))
                    # Files a click starts are counted, not kept: Playwright deletes them with the context.

                    def on_download(_download):
                        traffic["downloads"] += 1

                    context.on("page", lambda p: p.on("download", on_download))
                    for existing in context.pages:
                        existing.on("download", on_download)
                context.on("response", lambda r: response_tasks.append(asyncio.ensure_future(inspect_response(r))))

                page = await _first_page(context)
                if public:
                    state["logged_in"] = True  # writes are captured, never sent
                    status(f"Browser window open on {manifest.name}. No login is needed.")
                    await page.goto(manifest.base_url, wait_until="domcontentloaded", timeout=30000)
                else:
                    status(f"Browser window open. Log in to {manifest.name} in the window.")
                    await page.goto(manifest.login_url)

                # 3. Wait for a verified login.
                observation = LoginObservation()
                deadline = time.monotonic() + timeout
                while not public:
                    if state["header_too_large"] is not None:
                        raise LoginError(state["header_too_large"]["message"])
                    if await _capture(context, page, manifest, observation=observation) is not None:
                        break
                    if time.monotonic() > deadline:
                        raise LoginError(login_not_verified_message(manifest, observation, timeout))
                    await asyncio.sleep(poll_seconds)
                if not public:
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
                    on_login_page = not public and current.path == (urlsplit(manifest.login_url).path or "/")
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

                # 5. Visits, breadth first. With depth > 1, links found on visited pages are
                # visited too (same host, same filters as menu links), up to max_pages in all.
                queue: list[tuple[object, int]] = [(v, 1) for v in [*visits, *chosen]]
                queued = {str(v) for v, _ in queue}
                followed = 0
                while queue:
                    visit, level = queue.pop(0)
                    path = valid_visit(visit, service_host)
                    if path is None:
                        summary["visited"].append({"path": str(visit)[:200], "status": "refused"})
                        continue
                    if sum(1 for v in summary["visited"] if v["status"] != "refused") >= max_pages:
                        summary["visited"].append({"path": path, "status": "skipped: max_pages"})
                        continue
                    url = urljoin(manifest.base_url.rstrip("/") + "/", path.lstrip("/"))
                    try:
                        await page.goto(url, wait_until="networkidle", timeout=30000)
                        summary["visited"].append({"path": path, "status": "ok", "depth": level})
                    except Exception as exc:  # keep going with the other pages
                        summary["visited"].append({"path": path, "status": f"error: {type(exc).__name__}"})
                        continue
                    if level < depth:
                        try:
                            found = filter_menu_links(await page.evaluate(COLLECT_JS), service_host,
                                                      logged_in=state["logged_in"])
                        except Exception:
                            found = {"links": []}
                        for link in found["links"]:
                            if link["path"] not in queued:
                                queued.add(link["path"])
                                queue.append((link["path"], level + 1))
                                followed += 1
                    if explore:
                        explored.append(await explore_page(page, url, path, traffic, click_wait, search_text))
                summary["links_followed"] = followed

                if explore:
                    _write_private(out_dir / "explored.json", explored)
                    summary["explored"] = {
                        "pages": [{k: v for k, v in e.items() if k not in ("controls", "searches")} for e in explored],
                        "file": str(out_dir / "explored.json"),
                    }
                    status("Explored {} pages: {} clicks, {} JSON reads and {} data-host requests after clicks."
                           .format(len(explored), sum(e["clicked"] for e in explored),
                                   sum(e["json_reads"] for e in explored),
                                   sum(e["data_host_requests"] for e in explored)))

                # Where the pages' apps keep their tokens: key names only, for data_auth.
                if state["logged_in"]:
                    try:
                        summary["token_storage"] = await page.evaluate(TOKEN_KEYS_JS)
                    except Exception:
                        summary["token_storage"] = []

                # 6. Suggested session checks: JSON endpoints the pages loaded, tested both ways.
                if suggest:
                    picks = [c for c in rank(seen_json) if c not in {e.get("path") for e in candidates}]
                    if picks:
                        cookies = await context.cookies([manifest.base_url])
                        agent = await _user_agent(page)
                        tested = []
                        for path in picks:
                            trial = manifest_with_check(manifest, path)
                            tested.append({
                                "path": path,
                                "logged_in": await check_status(trial, cookies, user_agent=agent),
                                "logged_out": await preflight_check(trial),
                            })
                        reports = [_candidate_report(entry) for entry in tested]
                        reports.sort(key=lambda r: not r["usable"])
                        summary["suggested_session_checks"] = reports
                        usable = [r["path"] for r in reports if r["usable"]]
                        status(f"Suggested session checks: {len(usable)} usable of {len(reports)} tested"
                               + (f" (best: {usable[0]})." if usable else "."))
                    else:
                        summary["suggested_session_checks"] = []
                        status("No JSON endpoints were seen after login; visit more pages to get suggestions.")

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
    # The analyzer refuses recordings whose login was not verified.
    _write_private(out_dir / "recording.json", {
        "service": manifest.name,
        "login_verified": summary["login_verified"],
        "public": public,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    })
    summary["captured_writes"] = len(captured)
    if har_path.exists():
        os.chmod(har_path, 0o600)  # cookies and page content: private to the user
    summary["har"] = str(har_path) if har_path.exists() else None
    summary["captured_file"] = str(captured_path)
    return summary


async def _close_extra(context, new_page) -> None:
    if context.pages and new_page is not context.pages[0]:
        try:
            await new_page.close()
        except Exception:
            pass


async def _settle(page, seconds: float) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=seconds * 1000)
    except Exception:
        pass
    await asyncio.sleep(0.3)


def _delta(traffic: dict, start: dict) -> dict:
    return {
        "requests": traffic["requests"] - start["requests"],
        "json_reads": traffic["json_reads"] - start["json_reads"],
        "data_host_requests": traffic["data_host"] - start["data_host"],
        "captured_writes": traffic["captured"] - start["captured"],
        "downloads": traffic["downloads"] - start["downloads"],
    }


async def _back_to(page, url: str, click_wait: float) -> None:
    if urlsplit(page.url).path != urlsplit(url).path:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _settle(page, click_wait)


async def decline_banner(page, click_wait: float) -> str | None:
    """Answer a cookie banner with its decline choice, if one is showing. Never accept."""
    try:
        target = decline_control(await page.evaluate(COLLECT_CONTROLS_JS))
        if target is None:
            return None
        await page.locator(f'[data-pmcp-explore="{target["id"]}"]').first.click(timeout=3000)
        await _settle(page, min(click_wait, 2))
        return "declined"
    except Exception as exc:
        return f"error: {type(exc).__name__}"


async def explore_searches(page, url: str, traffic: dict, click_wait: float, text: str) -> list[dict]:
    """Fill each search field with the probe text and submit it, one at a time."""
    results: list[dict] = []
    try:
        fields = search_fields(await page.evaluate(COLLECT_SEARCH_JS))
    except Exception:
        return results
    for index, field in enumerate(fields):
        if index:
            await _back_to(page, url, click_wait)
            try:
                field = next(f for f in search_fields(await page.evaluate(COLLECT_SEARCH_JS)) if f["hint"] == field["hint"])
            except (StopIteration, Exception):
                continue
        start = dict(traffic)
        outcome = "ok"
        try:
            box = page.locator(f'[data-pmcp-search="{field["id"]}"]').first
            await box.fill(text, timeout=3000)
            await _settle(page, 1.5)  # suggestions while typing
            await box.press("Enter", timeout=3000)
            await _settle(page, click_wait)
        except Exception as exc:
            outcome = f"error: {type(exc).__name__}"
        results.append({"field": field.get("hint", "")[:80], "form_method": field.get("form_method"),
                        "outcome": outcome, "left_page": urlsplit(page.url).path != urlsplit(url).path,
                        **_delta(traffic, start)})
    await _back_to(page, url, click_wait)
    return results


async def explore_page(page, url: str, path: str, traffic: dict, click_wait: float,
                       search_text: str = "test") -> dict:
    """Fill search fields, then click the page's safe controls one at a time, and note what
    each step caused."""
    def dismiss(dialog):
        asyncio.ensure_future(dialog.dismiss())

    page.on("dialog", dismiss)
    before_page = dict(traffic)
    consent = await decline_banner(page, click_wait)
    searches = await explore_searches(page, url, traffic, click_wait, search_text) if search_text else []
    clicked: set[str] = set()
    skipped: dict[str, str] = {}
    controls: list[dict] = []
    try:
        while len(clicked) < MAX_CLICKS_PER_PAGE:
            if urlsplit(page.url).path != urlsplit(url).path:
                # The last click moved away: back to the visited page before the next one.
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await _settle(page, click_wait)
            try:
                raw = await page.evaluate(COLLECT_CONTROLS_JS)
            except Exception:
                break
            safe, newly_skipped = pick_controls(raw, clicked)
            skipped.update({k: v for k, v in newly_skipped.items() if k not in clicked})
            if not safe:
                break
            target = safe[0]
            key = control_key(target)
            clicked.add(key)
            skipped.pop(key, None)
            start = dict(traffic)
            outcome = "ok"
            try:
                await page.locator(f'[data-pmcp-explore="{target["id"]}"]').first.click(timeout=3000)
                await _settle(page, click_wait)
            except Exception as exc:
                outcome = f"error: {type(exc).__name__}"
            controls.append({
                "text": target.get("text", "") or "(icon that expands)",
                "kind": target.get("role") or target.get("tag"),
                "outcome": outcome,
                **_delta(traffic, start),
                "left_page": urlsplit(page.url).path != urlsplit(url).path,
            })
    finally:
        page.remove_listener("dialog", dismiss)
    reasons: dict[str, int] = {}
    for reason in skipped.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "path": path,
        "consent_banner": consent,
        "searched": len(searches),
        "clicked": len(controls),
        "skipped": reasons,
        **_delta(traffic, before_page),
        "searches": searches,
        "controls": controls,
    }


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
    parser.add_argument("--suggest-check", action="store_true",
                        help="suggest session checks from the recording even if the current one answers 401")
    parser.add_argument("--check-candidate", action="append", default=[],
                        help="an endpoint to try as session.check, logged out and logged in (repeatable)")
    parser.add_argument("--reuse-profile", action="store_true",
                        help="use the persistent browser profile instead of a clean one (only if the user chose it)")
    parser.add_argument("--explore", action="store_true",
                        help="on each visited page, click safe controls (tabs, sections, show more) itself")
    parser.add_argument("--depth", type=int, default=None,
                        help="follow links found on visited pages this many levels deep (default 2 with --discover)")
    parser.add_argument("--max-pages", type=int, default=100, help="visit at most this many pages in all")
    parser.add_argument("--search-text", default="test",
                        help="text --explore types into search fields ('' to leave them alone)")
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
        suggest_check=args.suggest_check,
        explore=args.explore,
        search_text=args.search_text,
        depth=args.depth if args.depth is not None else (2 if args.discover else 1),
        max_pages=args.max_pages,
    ))
    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    if summary.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
