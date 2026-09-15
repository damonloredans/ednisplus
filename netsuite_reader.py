"""Reads the real tracking number off a NetSuite Sales Order's Shipping
subtab — the source of truth. eDesk's own "TRACKING NO." field has been
observed to disagree with NetSuite on some orders, so the reply drafter never
trusts eDesk for this; it always looks it up here instead.

Connection/tab-discovery pattern (CDP on port 9222, global-search box
detection) mirrors ednis/netsuite_bridge.py, replicated here rather than
imported so this tool has no dependency on the ednis/ folder. Unlike
netsuite_bridge.py — which only ever follows search-result hrefs and never
reads a field *value* — this module opens the Sales Order and reads the
Tracking # field's actual text.
"""

from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

from chrome_utils import open_background_tab

CDP_URL = "http://localhost:9222"

SEARCH_INPUT_SELECTORS = [
    "input[placeholder='Search']",
    "input[placeholder*='Search' i]",
    "#nsSearchField",
    "input[title='Search']",
]

# Text NetSuite shows on the Shipping subtab's Tracking # field when the
# order hasn't shipped yet. The user described it as literally "non" — kept
# alongside the more standard spellings in case the exact wording varies.
NOT_SHIPPED_VALUES = {"non", "none", "n/a", "na", ""}

# NOTE: selectors below for the Shipping subtab and the Tracking # field are
# a best-effort first pass, following the same "find the label, read the
# value near it" approach that already works elsewhere in both codebases —
# they haven't been verified against a live Sales Order yet and likely need
# adjusting once tested.
SHIPPING_SUBTAB_SELECTORS = [
    "a:has-text('Shipping')",
    "li:has-text('Shipping')",
    "[id*='shipping' i]:has-text('Shipping')",
]
TRACKING_LABEL_TEXT = "Tracking"


def _search_box(page):
    for sel in SEARCH_INPUT_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


def _find_netsuite_page(context):
    ns_pages = [p for p in context.pages if "netsuite.com" in p.url]
    with_search = [p for p in ns_pages if _search_box(p) is not None]
    if not with_search:
        return None
    for p in with_search:
        try:
            if p.evaluate("document.visibilityState") == "visible":
                return p
        except Exception:
            continue
    return with_search[0]


def _connect():
    try:
        p = sync_playwright().start()
        browser = p.chromium.connect_over_cdp(CDP_URL)
    except Exception as e:
        raise RuntimeError(
            "Can't reach Chrome on port 9222. Run ednis/launch_chrome_debug.bat "
            "first and make sure a NetSuite tab is open in that window."
        ) from e
    return p, browser


def _get_netsuite_page(browser):
    context = browser.contexts[0]
    page = _find_netsuite_page(context)
    if page is None:
        raise RuntimeError(
            "No NetSuite tab with the global search bar. Open your NetSuite "
            "dashboard (or any standard record page) in the automation window."
        )
    return context, page


def _type_into_search(page, query: str, log):
    log(f"Found NetSuite tab: {page.url}")
    search_box = _search_box(page)
    if search_box is None:
        raise RuntimeError(
            "Could not find the NetSuite global search box (selectors need "
            "adjusting)."
        )
    search_box.click()
    search_box.fill("")
    search_box.type(query, delay=40)
    log(f"Typed '{query}', waiting for results...")
    page.wait_for_timeout(1000)


def _open_sales_order(context, page, query: str, log):
    """Searches for `query` and opens the first matching Sales Order in a new
    background tab. Returns the new tab's Page."""
    result = page.locator("text=/^Sales Order:/").first
    try:
        result.wait_for(timeout=8000)
    except Exception as e:
        raise RuntimeError(
            f"No Sales Order result showed up for '{query}'. Double-check the "
            "order number."
        ) from e

    anchor = result.locator("xpath=ancestor-or-self::a[1]")
    href = anchor.get_attribute("href")
    if not href:
        raise RuntimeError("Found the Sales Order result but couldn't get its link.")
    so_url = urljoin(page.url, href)

    page.keyboard.press("Escape")

    log("Opening the Sales Order to read its Shipping tab...")
    so_page = open_background_tab(context, so_url)
    so_page.wait_for_load_state("domcontentloaded")
    return so_page


def _read_tracking_field(so_page, log):
    for sel in SHIPPING_SUBTAB_SELECTORS:
        try:
            tab = so_page.locator(sel).first
            if tab.count() > 0 or tab.is_visible(timeout=500):
                tab.click()
                so_page.wait_for_timeout(600)
                break
        except Exception:
            continue
    else:
        log("Couldn't find a Shipping subtab (selectors need adjusting).")
        return []

    label = so_page.get_by_text(TRACKING_LABEL_TEXT, exact=False)
    if label.count() == 0:
        log("Couldn't find a Tracking # field on the Shipping subtab.")
        return []

    for xpath in (
        "xpath=ancestor::div[1]",
        "xpath=ancestor::div[2]",
        "xpath=following::*[1]",
        "xpath=following::*[2]",
    ):
        try:
            raw = label.first.locator(xpath).inner_text(timeout=1000).strip()
        except Exception:
            continue
        if not raw:
            continue
        cleaned = raw.replace(TRACKING_LABEL_TEXT, "").strip(" : ")
        if not cleaned:
            continue
        if cleaned.lower() in NOT_SHIPPED_VALUES:
            return []
        # Multi-package orders sometimes pack several tracking numbers onto
        # one line, space-separated — split and drop any stray non-shipped
        # tokens rather than treating the whole line as one value.
        numbers = [tok for tok in cleaned.split() if tok.lower() not in NOT_SHIPPED_VALUES]
        if numbers:
            return numbers
    return []


def read_tracking_numbers(order_query: str, log=print) -> list[str]:
    """Looks up `order_query`'s Sales Order in NetSuite and returns every
    Tracking # from its Shipping subtab (usually one, sometimes several for
    a multi-package shipment) — empty if it hasn't shipped yet, or the field
    couldn't be found/read."""
    p, browser = _connect()
    try:
        context, page = _get_netsuite_page(browser)
        _type_into_search(page, order_query, log)
        so_page = _open_sales_order(context, page, order_query, log)
        tracking_numbers = _read_tracking_field(so_page, log)
        if tracking_numbers:
            log(f"Tracking #(s) from NetSuite: {', '.join(tracking_numbers)}")
        else:
            log("No tracking # on NetSuite yet — order likely hasn't shipped.")
        return tracking_numbers
    finally:
        # Do NOT call browser.close(): this is the user's real, already-
        # running Chrome window, not one Playwright launched itself.
        p.stop()
