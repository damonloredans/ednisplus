"""Reads the full context of the currently open eDesk ticket — subject,
customer message text, order number, ecom number — for the reply drafter.

Connection/tab-discovery pattern is the same one ednis/edesk_bridge.py uses
(CDP connect on port 9222, TICKET_URL_RE tab matching, a resolved-`page`
object, an optional picker when several tickets are open), replicated here
rather than imported so this tool has no dependency on the ednis/ folder.

Unlike ednis/edesk_bridge.py, this module never reads a tracking number off
the ticket — that comes exclusively from NetSuite (see netsuite_reader.py),
since the two have been observed to disagree.
"""

import re

from playwright.sync_api import sync_playwright

from order_parser import parse_order_number

CDP_URL = "http://localhost:9222"

ECOM_NUMBER_RE = re.compile(r"#\s*(\d+)")

# Individual ticket pages look like dashboard-3.edesk.com/crm/view/715417337 —
# excludes list/inbox views like /crm/new or /crm/todo.
TICKET_URL_RE = re.compile(r"edesk\.com/crm/view/\d+")

# How much of the captured page text to hand to Gemini. Broad first pass (see
# _read_subject_and_body below) — generous but bounded so a long ticket
# thread doesn't blow out the prompt.
MAX_BODY_CHARS = 6000


class NoTicketTabsError(RuntimeError):
    pass


def _find_edesk_pages(context):
    return [p for p in context.pages if TICKET_URL_RE.search(p.url)]


def _read_order_number(page):
    """Same approach as ednis/edesk_bridge.py: read the value next to the
    ORDER NO. label, never scan the whole page for it (that previously
    matched unrelated '#'-prefixed text like a product SKU)."""
    label = page.get_by_text("ORDER NO.", exact=False)
    if label.count() == 0:
        return None
    for xpath in ("xpath=ancestor::div[1]", "xpath=ancestor::div[2]", "xpath=ancestor::div[3]"):
        try:
            container_text = label.first.locator(xpath).inner_text(timeout=1000)
        except Exception:
            continue
        query = parse_order_number(container_text)
        if query:
            return query
    return None


def _read_ecom_number(page):
    try:
        page_text = page.inner_text("body")
    except Exception:
        return None
    m = ECOM_NUMBER_RE.search(page_text)
    return m.group(1) if m else None


# NOTE: the exact eDesk DOM structure for the subject line and the customer's
# message/question text isn't mapped anywhere in either codebase yet — ednis's
# own reader only ever targets the labelled ORDER NO. / TRACKING NO. fields.
# This starts broad (the ticket page's visible body text) and should be
# narrowed to just the subject + latest customer message once tested against
# a real ticket, if the broad text proves too noisy for Gemini to work with.
def _read_subject_and_body(page):
    try:
        page_text = page.inner_text("body")
    except Exception:
        return "", ""

    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    subject = lines[0] if lines else ""
    body = page_text.strip()[:MAX_BODY_CHARS]
    return subject, body


def _page_label(page):
    m = re.search(r"/view/(\d+)", page.url)
    ticket_id = m.group(1) if m else "?"
    try:
        order = _read_order_number(page)
    except Exception:
        order = None
    return f"Ticket {ticket_id} — {order}" if order else f"Ticket {ticket_id}"


def _resolve_edesk_page(context, pick_page):
    candidates = _find_edesk_pages(context)
    if not candidates:
        raise NoTicketTabsError(
            "No eDesk tab found in the automation Chrome window. Open the "
            "ticket there first."
        )
    if len(candidates) == 1 or pick_page is None:
        return candidates[0]

    labels = [_page_label(p) for p in candidates]
    idx = pick_page(labels)
    if idx is None:
        raise RuntimeError("Cancelled — no ticket selected.")
    return candidates[idx]


def read_ticket(log=print, pick_page=None) -> dict:
    """Returns {subject, body_text, order_query, ecom_number} for the
    resolved eDesk ticket tab. Deliberately excludes any tracking info."""
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            raise RuntimeError(
                "Can't reach Chrome on port 9222. Run "
                "ednis/launch_chrome_debug.bat first and make sure a ticket "
                "is open in that window."
            ) from e

        context = browser.contexts[0]
        page = _resolve_edesk_page(context, pick_page)

        log(f"Reading ticket: {page.url}")
        subject, body_text = _read_subject_and_body(page)
        order_query = _read_order_number(page)
        ecom_number = _read_ecom_number(page)

        return {
            "subject": subject,
            "body_text": body_text,
            "order_query": order_query,
            "ecom_number": ecom_number,
        }
