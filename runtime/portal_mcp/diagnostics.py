"""Diagnostics shared by the runtime and the creator skill.

- Recognize "request headers too large" answers.
- Summarize cookies by host and name pattern, never by value.
- Recognize third-party tracking hosts, so blocked lists can be split.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

# Phrases servers use when the request headers (usually the Cookie header) are too large.
_HEADER_TOO_LARGE = re.compile(
    r"request header(s)? (or cookie )?(is |are )?too (large|long)"
    r"|size of the request headers is too long"
    r"|request too long"
    r"|header(s)? too large"
    r"|request header fields too large"
    r"|bad request - header",
    re.IGNORECASE,
)


def is_header_too_large(status: int | None, body: str | None = None, reason: str | None = None) -> bool:
    """True for HTTP 431, or 400/413/494 whose text says the headers or cookies are too large."""
    if status == 431:
        return True
    if status not in (400, 413, 494):
        return False
    text = f"{reason or ''} {body or ''}"
    return bool(_HEADER_TOO_LARGE.search(text))


_ID_PART = re.compile(r"[0-9A-Fa-f]{12,}|[A-Za-z0-9%+/=_-]{20,}")


def name_pattern(name: str) -> str:
    """Collapse random parts of a cookie name, e.g. "nonce.Ab12...%3D" -> "nonce.<id>"."""
    collapsed = _ID_PART.sub("<id>", name)
    return re.sub(r"(<id>)+", "<id>", collapsed)


def cookie_summary(cookies: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Count cookies per host and per name pattern. Values are never read into the result."""
    cookies = list(cookies)
    by_host: Counter[str] = Counter()
    patterns: Counter[str] = Counter()
    approx_bytes = 0
    for cookie in cookies:
        host = str(cookie.get("domain", "")).lstrip(".")
        by_host[host] += 1
        patterns[name_pattern(str(cookie.get("name", "")))] += 1
        approx_bytes += len(str(cookie.get("name", ""))) + len(str(cookie.get("value", ""))) + 3
    return {
        "count": len(cookies),
        "approx_cookie_header_bytes": approx_bytes,
        "by_host": dict(by_host.most_common()),
        "by_name_pattern": dict(patterns.most_common()),
    }


def header_too_large_message(service: str, host: str, cookies: Iterable[dict[str, Any]]) -> str:
    summary = cookie_summary(cookies)
    top = ", ".join(f"{name} x{count}" for name, count in list(summary["by_name_pattern"].items())[:6])
    return (
        f"{service} rejected the request because its headers were too large "
        f"(the Cookie header for {host} is about {summary['approx_cookie_header_bytes']} bytes, "
        f"{summary['count']} cookies; most common: {top or 'none'}). "
        "This usually means the browser profile has collected too many cookies. "
        "Start again with a clean profile."
    )


# Third-party tracking, analytics, monitoring and consent services. Blocking them is expected
# and harmless; they are listed apart from other blocked hosts to make troubleshooting easier.
TRACKING_DOMAINS = (
    "googletagmanager.com",
    "google-analytics.com",
    "analytics.google.com",
    "doubleclick.net",
    "googleadservices.com",
    "googlesyndication.com",
    "applicationinsights.azure.com",
    "applicationinsights.microsoft.com",
    "dc.services.visualstudio.com",
    "js.monitor.azure.com",
    "monitor.azure.com",
    "hotjar.com",
    "hotjar.io",
    "clarity.ms",
    "bat.bing.com",
    "facebook.net",
    "connect.facebook.net",
    "snap.licdn.com",
    "px.ads.linkedin.com",
    "matomo.cloud",
    "segment.io",
    "segment.com",
    "mixpanel.com",
    "newrelic.com",
    "nr-data.net",
    "sentry.io",
    "datadoghq.com",
    "browser-intake-datadoghq.com",
    "siteimproveanalytics.com",
    "siteimprove.com",
)


# Consent managers: the cookie banners. Some pages show nothing until the banner has loaded
# and been answered, so recordings may load them (GET only) and decline. They are still
# tracking-adjacent, so they can never be declared as hosts in a manifest.
CONSENT_DOMAINS = (
    "cookiebot.com",
    "cookielaw.org",
    "onetrust.com",
    "consensu.org",
    "usercentrics.eu",
    "usercentrics.com",
    "didomi.io",
    "cookiefirst.com",
    "cookieinformation.com",
    "consentmanager.net",
    "iubenda.com",
    "trustarc.com",
    "termly.io",
    "cookieyes.com",
)


def _matches(host: str, domains: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def is_consent_host(host: str) -> bool:
    return _matches(host, CONSENT_DOMAINS)


def is_tracking_host(host: str) -> bool:
    """Tracking, analytics and monitoring hosts, and consent managers (never declarable)."""
    return _matches(host, TRACKING_DOMAINS) or is_consent_host(host)
