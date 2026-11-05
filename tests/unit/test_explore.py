"""Which controls the recorder's --explore may click."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from explore import pick_controls, skip_reason  # noqa: E402


def control(text, **extra):
    item = {"id": "0", "tag": "button", "role": "", "type": "button", "in_form": False, "text": text,
            "disabled": False, "visible": True, "href": None, "target": "", "download": False}
    item.update(extra)
    return item


def test_tabs_and_sections_are_safe():
    assert skip_reason(control("Open cases", role="tab")) is None
    assert skip_reason(control("Show more")) is None
    assert skip_reason(control("Visa fler")) is None
    assert skip_reason(control("Logga in")) is None  # an app's own sign-in that gets a token
    assert skip_reason(control("More", tag="summary", type="")) is None
    assert skip_reason(control("Details", tag="a", href="#details")) is None


def test_actions_are_never_clicked():
    for text in ("Log out", "Logga ut", "Delete all", "Ta bort", "Betala", "Skicka", "Spara",
                 "Bekräfta", "Godkänn", "Avbryt", "Cancel", "Pay now", "Send message", "OK", "Ja"):
        assert skip_reason(control(text)) == "looks like an action", text


def test_forms_links_and_unlabeled_controls_are_skipped():
    assert skip_reason(control("Next", type="submit", in_form=True)) == "submits a form"
    assert skip_reason(control("Next", type="button", in_form=True)) is None
    assert skip_reason(control("")) == "no label"
    assert skip_reason(control("Show", visible=False)) == "hidden"
    assert skip_reason(control("Show", disabled=True)) == "disabled"
    assert skip_reason(control("Invoices", tag="a", href="/invoices")) == "link to another page"
    assert skip_reason(control("Help", tag="a", href="#", target="_blank")) == "opens a window or downloads"
    assert skip_reason(control("File", tag="a", href="#", download=True)) == "opens a window or downloads"


def test_each_control_is_picked_once():
    raw = [control("Show more", id="1"), control("Show more", id="2"), control("Delete", id="3")]
    safe, skipped = pick_controls(raw, set())
    assert [c["id"] for c in safe] == ["1"]
    assert skipped == {"button|Delete": "looks like an action"}
    safe, _ = pick_controls(raw, {"button|Show more"})
    assert safe == []
