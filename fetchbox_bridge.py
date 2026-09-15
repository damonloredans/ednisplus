"""Fetches the user's Fetchbox reply templates and renders them exactly the
way Fetchbox itself would.

Fetchbox (../quico/RAP-Stickies) is backed by a public Google Sheet — its own
server (../quico/server/src/scripts.js) proxies the sheet's gviz endpoint
with a plain, unauthenticated fetch(), which confirms the sheet is public.
So this tool reads the same sheet directly; no Fetchbox login/JWT needed.

The record shape and the placeholder-rendering rules below are a direct
Python port of ../quico/RAP-Stickies/index.html's gvizToRecords/
rowsToRecords/makeId (:909-915, 884-901) and its placeholder pipeline —
getPlaceholders/SELECT_FIELDS/REPEAT_GROUPS/buildRepeatingScript/
fillPlaceholders/applyName (:1275-1351) — kept in lockstep so a script
renders identically whether it's copied from Fetchbox or drafted here.
"""

import json
import re

import requests

SHEET_ID = "1AzZu38B86DIxJCTwg_n5bDsr7bLYXHE5BMY-kTWQ9dA"
GID = "0"
GVIZ_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:json&gid={GID}"

# ---- fetch + parse (mirrors gvizToRecords / rowsToRecords / makeId) -------


def _fetch_gviz_json() -> dict:
    resp = requests.get(GVIZ_URL, timeout=15)
    resp.raise_for_status()
    text = resp.text
    # Wrapped in `google.visualization.Query.setResponse(...)` plus a leading
    # comment line, same as scripts.js has to unwrap.
    start = text.find("(")
    end = text.rfind(")")
    if start == -1 or end == -1:
        raise RuntimeError("Unexpected sheet response format")
    return json.loads(text[start + 1 : end])


def _gviz_to_rows(gviz: dict) -> list[list[str]]:
    table = gviz.get("table") or {}
    raw_rows = table.get("rows") or []
    rows = []
    for r in raw_rows:
        cells = r.get("c") or []
        rows.append(["" if c is None or c.get("v") is None else str(c["v"]) for c in cells])
    return rows


def _rows_to_records(rows: list[list[str]]) -> list[dict]:
    # The sheet's real header ends up as row 0 of the data (not table.cols) —
    # same quirk the Fetchbox frontend works around.
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    records = []
    for row in rows[1:]:
        if not any(c and c.strip() for c in row):
            continue
        rec = {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(header)}
        records.append(rec)
    return records


def _to_int32(n: int) -> int:
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n >= 0x80000000 else n


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(digits[r])
    return "".join(reversed(out))


def make_id(rec: dict) -> str:
    """Same id scheme as Fetchbox's makeId, so ids stay comparable if ever
    cross-referenced: a 32-bit string hash of `cat_final|script`."""
    s = (rec.get("cat_final") or "") + "|" + (rec.get("script") or "")
    h = 0
    for ch in s:
        h = _to_int32(h * 31 + ord(ch))
    return "r" + _base36(h & 0xFFFFFFFF)


def fetch_scripts(log=print) -> list[dict]:
    log("Fetching Fetchbox templates from Google Sheets...")
    gviz = _fetch_gviz_json()
    records = _rows_to_records(_gviz_to_rows(gviz))
    for rec in records:
        rec["id"] = make_id(rec)
    log(f"Loaded {len(records)} templates.")
    return records


# ---- placeholder rendering (mirrors the Fetchbox pipeline) ----------------

SELECT_FIELDS = {
    "_CARRIER_": ["Amazon Shipping", "FedEx", "UPS", "USPS", "Other"],
    "_PLATFORM_": ["Amazon", "eBay", "Walmart", "Other"],
    "_CLAIMTYPE_": [
        "damaged item",
        "damaged/defective item",
        "defective item",
        "missing item",
        "shipping",
        "warranty",
        "wrong item",
        "Other",
    ],
    "_DELSTATUS_": ["Delivered", "In Transit", "On the Way", "Other"],
    "_NUMSHIPMENTS_": ["1", "2", "3", "4"],
}

REPEAT_GROUPS = {
    "_NUMSHIPMENTS_": {"max": 4, "item_suffixes": ["CARRIER", "TRACKINGNUM", "DELSTATUS", "DETAILS"]},
}

DEFAULT_NAME = "your-name"

_PLACEHOLDER_RE = re.compile(r"_[A-Z0-9]+_")


def get_placeholders(script: str) -> list[str]:
    seen = []
    for m in _PLACEHOLDER_RE.finditer(script):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


def build_repeating_script(script: str, placeholder_values: dict) -> str:
    count_token = next((tok for tok in REPEAT_GROUPS if tok in script), None)
    if not count_token:
        return script
    group = REPEAT_GROUPS[count_token]
    max_n, item_suffixes = group["max"], group["item_suffixes"]

    block_re = re.compile(r"_(\d+)(" + "|".join(item_suffixes) + r")_")
    matches = [
        {"index": m.start(), "end": m.end(), "num": int(m.group(1)), "suffix": m.group(2)}
        for m in block_re.finditer(script)
    ]
    if not matches:
        return script

    block_nums = sorted({m["num"] for m in matches})

    def set_of(num):
        return {m["suffix"] for m in matches if m["num"] == num}

    used_suffixes = [s for s in item_suffixes if all(s in set_of(num) for num in block_nums)]
    if not used_suffixes:
        return script
    per_block_len = len(used_suffixes)

    kept = sorted((m for m in matches if m["suffix"] in used_suffixes), key=lambda m: m["index"])
    if len(kept) != len(block_nums) * per_block_len:
        return script  # uneven — bail out safely, same as the JS version

    blocks = [[m for m in kept if m["num"] == num] for num in block_nums]
    existing_count = len(block_nums)
    chosen_raw = (placeholder_values or {}).get(count_token)
    try:
        chosen = int(chosen_raw) if chosen_raw else existing_count
    except (TypeError, ValueError):
        chosen = existing_count
    n = min(max(chosen, 1), max_n)

    first, last = blocks[0], blocks[-1]
    prefix = script[: first[0]["index"]]
    suffix = script[last[-1]["end"] :]
    sep = script[first[-1]["end"] : blocks[1][0]["index"]] if existing_count > 1 else ""

    def body_of(b):
        return script[b[0]["index"] : b[-1]["end"]]

    last_existing_body = body_of(blocks[existing_count - 1])
    renumber_re = re.compile(rf"_{existing_count}([A-Z]+)")
    bodies = []
    for k in range(1, n + 1):
        if k <= existing_count:
            bodies.append(body_of(blocks[k - 1]))
        else:
            bodies.append(renumber_re.sub(rf"_{k}\1", last_existing_body))
    return prefix + sep.join(bodies) + suffix


def apply_name(text: str, name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        return text
    return re.sub(r"\b" + re.escape(DEFAULT_NAME) + r"\b", name, text)


def fill_placeholders(script: str, values: dict, name: str | None = None) -> str:
    out = script
    for tok, val in (values or {}).items():
        if val:
            out = out.replace(tok, val)
    return apply_name(out, name)


def render_script(script: str, placeholder_values: dict, name: str | None = None) -> str:
    """Full pipeline: expand any repeating shipment blocks, then fill in
    every placeholder value, then sign it. This is what Fetchbox's copy
    button produces, reproduced exactly."""
    expanded = build_repeating_script(script, placeholder_values)
    return fill_placeholders(expanded, placeholder_values, name)
