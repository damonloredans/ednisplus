"""Writes the drafted reply into eDesk's own reply box, so the user reviews
it in eDesk's native editor and hits Send themselves — this module (and this
tool generally) never clicks Send.

Kept separate from edesk_reader.py (read-only) since this one modifies page
content. Re-connects independently rather than reusing edesk_reader's
Playwright session (same pattern as every other module here — each public
function opens its own short-lived CDP connection) and re-finds the same
ticket tab by URL so it targets the exact ticket read_ticket() already
resolved, without re-prompting a ticket picker.

NOTE: selectors below for the Template dropdown and the reply editor are a
first, unverified pass — eDesk's composer DOM isn't mapped anywhere yet;
expect to adjust once tested against a real ticket.
"""

import re

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"
TICKET_ID_RE = re.compile(r"/view/(\d+)")

TEMPLATE_DROPDOWN_SELECTORS = [
    "text=Template:",
]
TEMPLATE_NONE_OPTION_SELECTORS = [
    "[role='option']:has-text('None')",
    "li:has-text('None')",
    "text=None",
]
REPLY_EDITOR_SELECTORS = [
    "[contenteditable='true']",
    ".ql-editor",  # Quill, a common rich-text editor for this kind of composer
    "[role='textbox']",
]


def _connect_to_ticket(ticket_url: str):
    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp(CDP_URL)
    except Exception as e:
        p.stop()
        raise RuntimeError(
            "Can't reach Chrome on port 9222. Run ednis/launch_chrome_debug.bat "
            "first."
        ) from e

    context = browser.contexts[0]
    # Match by ticket id rather than an exact URL string — eDesk's URL can
    # pick up query params/hash changes between the read and this insert
    # (e.g. from expanding a panel), which would break an exact match.
    m = TICKET_ID_RE.search(ticket_url)
    page = None
    if m:
        needle = f"/view/{m.group(1)}"
        page = next((pg for pg in context.pages if needle in pg.url), None)
    if page is None:
        page = next((pg for pg in context.pages if pg.url == ticket_url), None)
    if page is None:
        p.stop()
        raise RuntimeError(
            "That eDesk ticket tab isn't open anymore — analyze the ticket again."
        )
    return p, page


def _reset_template_to_none(page, log) -> bool:
    """Best-effort: opens the Template dropdown and picks "None" first, so
    an auto-applied template doesn't collide with our own draft. Not fatal
    if this can't be found — the draft still gets inserted either way."""
    for sel in TEMPLATE_DROPDOWN_SELECTORS:
        try:
            dropdown = page.locator(sel).first
            if dropdown.count() == 0:
                continue
            dropdown.click()
            page.wait_for_timeout(300)
            for opt_sel in TEMPLATE_NONE_OPTION_SELECTORS:
                opt = page.locator(opt_sel).first
                if opt.count() > 0:
                    opt.click()
                    page.wait_for_timeout(300)
                    log("Reset the eDesk Template dropdown to None.")
                    return True
        except Exception:
            continue
    log("Couldn't find/reset the Template dropdown (selectors need adjusting) — inserting the draft as-is.")
    return False


def _find_reply_editor(page, log):
    """A ticket page can have several contenteditable regions (an internal
    Note editor, small inline-editable fields, ...) — picking the first DOM
    match risks typing into the wrong one invisibly. Instead, scan every
    match across all selectors and take the largest *visible* one, on the
    assumption that the actual reply body is the biggest editable area on
    the page. Returns None if nothing plausible turns up."""
    best, best_area = None, 0
    for sel in REPLY_EDITOR_SELECTORS:
        try:
            candidates = page.locator(sel)
            count = candidates.count()
        except Exception:
            continue
        for i in range(min(count, 20)):
            el = candidates.nth(i)
            try:
                if not el.is_visible():
                    continue
                box = el.bounding_box()
                if not box:
                    continue
                area = box["width"] * box["height"]
            except Exception:
                continue
            if area > best_area:
                best, best_area = el, area

    if best is None:
        log(f"Scanned {len(REPLY_EDITOR_SELECTORS)} selector(s), found no visible contenteditable region.")
        return None
    log(f"Using the largest visible editable region found ({int(best_area)}px² area).")
    return best


def insert_draft(ticket_url: str, draft_text: str, log=print) -> bool:
    """Resets the ticket's Template dropdown to None, clears whatever's
    currently in the reply editor, and types in `draft_text`. Never touches
    Send — the human reviews and sends it themselves in eDesk. Returns True
    if the draft was inserted, False if the reply editor couldn't be found
    (caller should fall back to Copy)."""
    p, page = _connect_to_ticket(ticket_url)
    try:
        log(f"Working on: {page.url}")
        _reset_template_to_none(page, log)

        editor = _find_reply_editor(page, log)
        if editor is None:
            log("Couldn't find the eDesk reply box (selectors need adjusting) — use Copy instead.")
            return False

        editor.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        editor.type(draft_text, delay=1)
        log("Inserted the draft into eDesk's reply box — review it there, then Send yourself.")
        return True
    finally:
        # Do NOT call browser.close(): this is the user's real, already-
        # running Chrome window, not one Playwright launched itself.
        p.stop()
