"""Cookie banners: consent managers load (GET only) during recordings and are declined."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from explore import decline_control  # noqa: E402
from guard import decide  # noqa: E402
from portal_mcp.diagnostics import is_consent_host, is_tracking_host  # noqa: E402
from portal_mcp.manifest import Manifest  # noqa: E402


def test_consent_managers_load_with_get_only():
    assert decide("GET", "https://consent.cookiebot.com/uc.js", "portal.example.com", True).allowed
    assert decide("GET", "https://cdn.cookielaw.org/consent/x.json", "portal.example.com", False).allowed
    assert not decide("POST", "https://consent.cookiebot.com/log", "portal.example.com", True).allowed
    assert not decide("GET", "http://consent.cookiebot.com/uc.js", "portal.example.com", True).allowed
    # Analytics stays blocked.
    assert not decide("GET", "https://www.googletagmanager.com/gtm.js", "portal.example.com", True).allowed


def test_consent_managers_still_cannot_be_declared():
    assert is_consent_host("app.usercentrics.eu") and is_tracking_host("app.usercentrics.eu")
    with pytest.raises(ValidationError, match="tracking"):
        Manifest.model_validate({"name": "p", "base_url": "https://portal.example.com", "session": {"type": "none"},
                                 "asset_hosts": ["consent.cookiebot.com"]})


def button(text, **extra):
    item = {"id": "0", "tag": "button", "text": text, "visible": True, "disabled": False}
    item.update(extra)
    return item


def test_the_decline_choice_is_picked_never_accept():
    for text in ("Endast nödvändiga", "Reject all", "Avvisa alla", "Only necessary cookies", "Neka"):
        assert decline_control([button("Acceptera alla", id="a"), button(text, id="d")])["id"] == "d", text
    assert decline_control([button("Accept all"), button("Settings")]) is None
    assert decline_control([button("Reject all", visible=False)]) is None
