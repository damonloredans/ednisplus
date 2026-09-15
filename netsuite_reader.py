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
                so_page.wait_for_timeout(1200)
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


# How much of each NetSuite record's visible text to hand to Gemini. Product
# fields (specs, thread size, part numbers, compatibility, ...) vary too much
# per listing to parse into fixed fields, so this reads the raw page text
# instead and lets Gemini pull out whatever's relevant, same "read broadly,
# let the model narrow it down" approach as edesk_reader's ticket body.
MAX_PRODUCT_DETAIL_CHARS = 4000


def _open_ecom_and_parent(context, page, ecom_number: str, log):
    """Searches for the raw ecom record number, opens the Ecom Record, then
    opens the link under its PARENT field too (same two-hop pattern as
    ednis/netsuite_bridge.py's open_ecom_record). Returns (ecom_page,
    parent_page_or_None) — a missing/unreadable PARENT link isn't fatal,
    since plenty of product detail can live on the Ecom Record itself."""
    result = page.locator("text=/^Ecom Record:/").first
    try:
        result.wait_for(timeout=8000)
    except Exception as e:
        raise RuntimeError(f"No 'Ecom Record' result showed up for '{ecom_number}'.") from e

    anchor = result.locator("xpath=ancestor-or-self::a[1]")
    href = anchor.get_attribute("href")
    if not href:
        raise RuntimeError("Found the Ecom Record result but couldn't get its link.")
    ecom_url = urljoin(page.url, href)

    page.keyboard.press("Escape")

    log("Opening the Ecom Record to read product details...")
    ecom_page = open_background_tab(context, ecom_url)
    ecom_page.wait_for_load_state("domcontentloaded")

    parent_page = None
    try:
        label = ecom_page.get_by_text("PARENT", exact=False).first
        parent_href = label.locator("xpath=following::a[1]").get_attribute("href", timeout=3000)
        if parent_href:
            log("Opening the PARENT record too...")
            parent_page = open_background_tab(context, urljoin(ecom_page.url, parent_href))
            parent_page.wait_for_load_state("domcontentloaded")
    except Exception:
        parent_page = None

    return ecom_page, parent_page


def _page_text(page, limit=MAX_PRODUCT_DETAIL_CHARS) -> str:
    """Reads the page's visible text, waiting past domcontentloaded first —
    NetSuite record pages render fields like TECH INFO via JS afterwards, so
    reading immediately on load can capture the page before they exist."""
    try:
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # NetSuite sometimes polls in the background and never
            # goes fully idle — the timeout below still gives fields a
            # chance to render.
        page.wait_for_timeout(1200)
        return page.inner_text("body").strip()[:limit]
    except Exception:
        return ""


# NOTE: like SHIPPING_SUBTAB_SELECTORS, best-effort and unverified live.
ITEMS_SUBTAB_SELECTORS = [
    "a:has-text('Items')",
    "li:has-text('Items')",
    "[id*='items' i]:has-text('Items')",
]


def _first_item_link(page):
    """Finds the first line-item hyperlink in the Items subtab's sublist —
    the ITEM column (e.g. 'B9NN17365B-OE'). Locates the sublist by its ITEM
    column header, then reads the first data row's link, mirroring the
    'find the label, read the value near it' approach used elsewhere."""
    try:
        header = page.get_by_text("ITEM", exact=True).first
        row = header.locator("xpath=ancestor::table[1]//tr[td][1]")
        href = row.locator("a").first.get_attribute("href", timeout=2000)
        if href:
            return urljoin(page.url, href)
    except Exception:
        pass
    return None


def _find_sales_order_or_cash_sale(context, page, order_query: str, log):
    """Returns (kind, page) for whichever of Sales Order / Cash Sale shows up
    in the current search results, opened in a new tab — or (None, None) if
    neither does. Cash Sale is NetSuite's record for orders paid immediately
    with no separate invoice step; checked as a fallback since not every
    order has a Sales Order."""
    for kind, pattern in (("Sales Order", "^Sales Order:"), ("Cash Sale", "^Cash Sale:")):
        result = page.locator(f"text=/{pattern}/").first
        try:
            result.wait_for(timeout=4000)
        except Exception:
            continue
        anchor = result.locator("xpath=ancestor-or-self::a[1]")
        href = anchor.get_attribute("href")
        if not href:
            continue
        url = urljoin(page.url, href)
        page.keyboard.press("Escape")
        log(f"Opening the {kind} to find the item...")
        txn_page = open_background_tab(context, url)
        txn_page.wait_for_load_state("domcontentloaded")
        return kind, txn_page
    return None, None


def _read_item_detail_from_transaction(context, txn_page, log) -> str:
    """On an opened Sales Order / Cash Sale, clicks into the Items subtab,
    opens the first line item's Inventory Item record, and returns its
    visible text — that's where specs/tech info actually live, not on the
    transaction record itself (per the user's own NetSuite screenshots)."""
    for sel in ITEMS_SUBTAB_SELECTORS:
        try:
            tab = txn_page.locator(sel).first
            if tab.count() > 0:
                tab.click()
                txn_page.wait_for_timeout(1200)
                break
        except Exception:
            continue

    item_url = _first_item_link(txn_page)
    if not item_url:
        log("Couldn't find an item link on the Items subtab.")
        return ""

    log("Opening the item to read its specs...")
    item_page = open_background_tab(context, item_url)
    item_page.wait_for_load_state("domcontentloaded")
    return _page_text(item_page)


def read_product_details(order_query: str | None = None, ecom_number: str | None = None, log=print) -> str:
    """Best-effort product/spec lookup, following the same priority a rep
    would: the Ecom Record (+ its PARENT) first when there's an ecom number,
    otherwise the Sales Order's line item, otherwise (no Sales Order tied to
    this order) the Cash Sale's line item — the latter two by clicking
    through to the actual Inventory Item record, since specs/tech info live
    there, not on the transaction record itself. Raw page text, not parsed
    fields — listings vary too much for fixed selectors, so Gemini pulls out
    whatever's relevant (thread size, OEM part number, fitment, ...) itself.
    Empty string if nothing could be found/read anywhere in the chain."""
    p, browser = _connect()
    try:
        context, page = _get_netsuite_page(browser)

        if ecom_number:
            try:
                _type_into_search(page, ecom_number, log)
                ecom_page, parent_page = _open_ecom_and_parent(context, page, ecom_number, log)
                parts = []
                ecom_text = _page_text(ecom_page)
                if ecom_text:
                    parts.append(f"ECOM RECORD:\n{ecom_text}")
                if parent_page is not None:
                    parent_text = _page_text(parent_page)
                    if parent_text:
                        parts.append(f"PARENT RECORD:\n{parent_text}")
                if parts:
                    log("Read product details from the Ecom Record.")
                    return "\n\n".join(parts)
                log("Ecom Record had nothing readable — trying the Sales Order/Cash Sale.")
            except Exception as e:
                log(f"No Ecom Record found ({e}) — trying the Sales Order/Cash Sale instead.")

        if order_query:
            _type_into_search(page, order_query, log)
            kind, txn_page = _find_sales_order_or_cash_sale(context, page, order_query, log)
            if txn_page is not None:
                item_text = _read_item_detail_from_transaction(context, txn_page, log)
                if item_text:
                    log(f"Read item specs via the {kind}.")
                    return f"ITEM DETAIL (via {kind}):\n{item_text}"
            else:
                log("No Sales Order or Cash Sale found for this order either.")

        return ""
    finally:
        # Do NOT call browser.close(): this is the user's real, already-
        # running Chrome window, not one Playwright launched itself.
        p.stop()


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
