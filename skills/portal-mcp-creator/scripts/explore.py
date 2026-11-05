"""Safe controls on a page, for the recorder's --explore. Pure logic, no browser needed.

Single-page apps often load their data only after the user opens a tab, expands a section or
presses "show more". The recorder clicks such controls itself, one at a time. Controls that
mean an action (log out, delete, pay, send, save, confirm...) are never clicked, and neither
are submit buttons, unlabeled controls, downloads or links that leave the page. The guard
still blocks and captures every write a click causes.
"""

from __future__ import annotations

import re

MAX_CLICKS_PER_PAGE = 20

# Texts that mean an action, in English and Swedish. A control whose text, label or title
# matches is never clicked.
ACTION_TEXT = re.compile(
    r"\b(log ?out|log ?off|sign ?out|logga ut|unsubscribe|avregistrera|avsluta|delete|remove|"
    r"radera|ta bort|cancel|avbryt|säg upp|pay|betala|send|skicka|submit|save|spara|confirm|"
    r"bekräfta|approve|godkänn|accept|acceptera|signera|sign document|order|beställ|book|boka|buy|köp|"
    r"purchase|apply|ansök|reset|återställ|clear|rensa|archive|arkivera|block|blockera|"
    r"report|anmäl|upload|ladda upp|download|ladda ner|print|skriv ut|share|dela|publish|"
    r"publicera|yes|ja|ok|close account|stäng konto|transfer|överför|swish|withdraw|ta ut)\b",
    re.IGNORECASE,
)

# Marks every candidate control with data-pmcp-explore and describes it; no values of form
# fields are read. Run with page.evaluate(COLLECT_CONTROLS_JS).
COLLECT_CONTROLS_JS = """() => {
  const selector = 'button, [role="button"], [role="tab"], [role="menuitem"], [aria-expanded], summary, a';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(selector)) {
    const id = String(n++);
    el.setAttribute('data-pmcp-explore', id);
    const rects = el.getClientRects();
    const style = window.getComputedStyle(el);
    const tag = el.tagName.toLowerCase();
    const href = tag === 'a' ? el.getAttribute('href') : null;
    out.push({
      id,
      tag,
      role: el.getAttribute('role') || '',
      type: (el.getAttribute('type') || (tag === 'button' ? 'submit' : '')).toLowerCase(),
      in_form: !!el.form || !!el.closest('form'),
      text: (el.innerText || el.getAttribute('aria-label') || el.title || el.value || '')
        .trim().replace(/\\s+/g, ' ').slice(0, 80),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      visible: rects.length > 0 && style.visibility !== 'hidden' && style.display !== 'none',
      href,
      target: el.getAttribute('target') || '',
      download: el.hasAttribute('download'),
    });
  }
  return out;
}"""


def control_key(item: dict) -> str:
    return f"{item.get('role') or item.get('tag')}|{item.get('text', '')}"


def skip_reason(item: dict) -> str | None:
    """Why a control must not be clicked, or None when it is safe to click."""
    text = str(item.get("text", "")).strip()
    tag = str(item.get("tag", ""))
    if not item.get("visible", False):
        return "hidden"
    if item.get("disabled"):
        return "disabled"
    if not text:
        return "no label"
    if ACTION_TEXT.search(text):
        return "looks like an action"
    if item.get("in_form") and item.get("type") in ("submit", "image", "reset"):
        return "submits a form"
    if tag == "a":
        href = item.get("href")
        if item.get("download") or (item.get("target") or "").lower() not in ("", "_self"):
            return "opens a window or downloads"
        if href not in (None, "") and not str(href).startswith("#"):
            return "link to another page"
    return None


def pick_controls(raw: list[dict], clicked: set[str]) -> tuple[list[dict], dict[str, str]]:
    """(safe controls not clicked yet, {key: reason} for the skipped ones). Duplicate keys
    count once: the same control text is clicked at most once per page."""
    safe: list[dict] = []
    skipped: dict[str, str] = {}
    seen: set[str] = set()
    for item in raw:
        key = control_key(item)
        if key in seen or key in clicked:
            continue
        seen.add(key)
        reason = skip_reason(item)
        if reason is None:
            safe.append(item)
        else:
            skipped[key] = reason
    return safe, skipped
