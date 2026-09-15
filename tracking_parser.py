"""Identifies a carrier from the *shape* of a tracking number, never from a
label someone typed. Tracking numbers here always come from NetSuite's Sales
Order Shipping subtab (see netsuite_reader.read_tracking) — deliberately not
from anything written on the eDesk ticket, since those two can disagree.

Patterns below are the user's own description of what each carrier's
tracking numbers look like ("usually"/"sometimes" — not a spec, just the
common cases), kept intentionally loose. detect_carrier() returns the first
match, so more specific patterns are listed first.
"""

import re

CARRIER_TRACKING_PATTERNS: dict[str, re.Pattern] = {
    # UPS: "1Z" + 6-char shipper number + 2-char service + 8-digit serial.
    "UPS": re.compile(r"^1Z[0-9A-Z]{16}$", re.IGNORECASE),
    # Amazon Logistics: "TBA" + digits/letters.
    "AMAZON": re.compile(r"^TBA[0-9A-Z]+$", re.IGNORECASE),
    # FedEx: starts with 8, 12 digits total.
    "FEDEX": re.compile(r"^8\d{11}$"),
    # USPS: usually 20-22 digits starting with 9; international sometimes
    # starts "CA" instead (e.g. CA123456785US-style).
    "USPS": re.compile(r"^9\d{19,21}$"),
    "USPS_INTL": re.compile(r"^CA\d{9}US$", re.IGNORECASE),
}


def detect_carrier(tracking_number: str | None) -> str | None:
    """Returns the carrier name whose pattern matches `tracking_number`, or
    None if nothing matches (including when `tracking_number` is None/empty).
    Never guesses — an unmatched number is shown as a bare tracking # with no
    carrier label rather than a wrong one."""
    if not tracking_number:
        return None
    cleaned = tracking_number.strip().replace(" ", "")
    for carrier, pattern in CARRIER_TRACKING_PATTERNS.items():
        if pattern.match(cleaned):
            return carrier
    return None
