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
    # Words that mostly show something are no longer taken for actions.
    for text in ("Order", "Ordrar", "Download", "Ladda ner", "Report", "Rapporter"):
        assert skip_reason(control(text)) is None, text
    # An icon without a label is clicked only when it expands or switches content.
    assert skip_reason(control("", expands=True)) is None


def test_actions_are_never_clicked():
    for text in ("Log out", "Logga ut", "Delete all", "Ta bort", "Betala", "Skicka", "Spara",
                 "Bekräfta", "Godkänn", "Avbryt", "Cancel", "Pay now", "Send message", "OK", "Ja",
                 "Place order", "Beställ", "Köp", "Print", "Dela"):
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


def test_search_fields_are_found_by_type_role_or_name():
    from explore import search_fields

    raw = [
        {"id": "0", "type": "search", "role": "", "hint": ""},
        {"id": "1", "type": "text", "role": "", "hint": "q  Sök på namn"},
        {"id": "2", "type": "text", "role": "", "hint": "email Your e-mail"},
        {"id": "3", "type": "text", "role": "searchbox", "hint": ""},
    ]
    assert [f["id"] for f in search_fields(raw)] == ["0", "1", "3"]
