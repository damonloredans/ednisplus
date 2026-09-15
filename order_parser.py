"""Replicated from ../ednis/order_parser.py (kept as a separate copy rather
than an import so this tool has no dependency on the ednis/ folder layout)."""

import re

SHOP_ORDER_RE = re.compile(r"#\s*(\d+)")
TRANSACTION_NO_RE = re.compile(r"\b(\d+-\d{4,}-\d{4,})\b")
# Walmart-style order numbers: a long bare digit run, no # or hyphens.
# Requires 12+ digits so it doesn't collide with 10-digit phone numbers.
RAW_LONG_NUMBER_RE = re.compile(r"\b(\d{12,})\b")


def parse_order_number(text: str) -> str | None:
    """Turn an eDesk order number into a NetSuite global-search query.

    '#248061'                    -> 'SHOP#248061'
    '24-14659-29759 (2811214)'   -> '24-14659-29759'
    '200014852796426'            -> '200014852796426' (Walmart, searched raw)
    """
    text = text.strip()

    m = SHOP_ORDER_RE.search(text)
    if m:
        return f"SHOP#{m.group(1)}"

    m = TRANSACTION_NO_RE.search(text)
    if m:
        return m.group(1)

    m = RAW_LONG_NUMBER_RE.search(text)
    if m:
        return m.group(1)

    return None
