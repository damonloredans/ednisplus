"""Picks the best-matching Fetchbox script for a ticket and figures out
placeholder values, using the free-tier Gemini API. A human always reviews
the result before it goes anywhere (see app.py) — this only drafts.
"""

import json
import os

import requests

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {
            "type": "STRING",
            "description": "2-4 plain-English sentences: what is the customer actually asking for?",
        },
        "script_id": {
            "type": "STRING",
            "description": "id of the single best-matching template from the candidate list.",
        },
        "placeholder_values": {
            "type": "ARRAY",
            "description": (
                "Values for as many of the chosen script's placeholder tokens as "
                "you can confidently work out from the ticket text, known facts, "
                "and product/item details. Make a genuine effort on every token — "
                "only omit one you truly have nothing to go on for; a human fills "
                "those remaining ones in by hand."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "token": {"type": "STRING"},
                    "value": {"type": "STRING"},
                },
                "required": ["token", "value"],
            },
        },
        "alternates": {
            "type": "ARRAY",
            "description": "Up to 2 other reasonable template matches, in case the top pick is wrong.",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "script_id": {"type": "STRING"},
                    "why": {"type": "STRING"},
                },
                "required": ["script_id", "why"],
            },
        },
    },
    "required": ["summary", "script_id", "placeholder_values", "alternates"],
}


def _format_known_fields(known_fields: dict) -> str:
    lines = []
    for k, v in known_fields.items():
        if k == "product_details" or v in (None, "", []):
            continue
        if k == "shipments" and isinstance(v, list):
            for i, s in enumerate(v, start=1):
                carrier = s.get("carrier") or "unknown carrier"
                lines.append(f"- shipment {i}: {carrier}, tracking # {s.get('tracking_number', '')}")
        else:
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def _build_prompt(ticket: dict, known_fields: dict, scripts: list[dict]) -> str:
    scripts_block = "\n\n".join(
        f"id: {s['id']}\ntitle: {s.get('cat1', '')}\ntags: {s.get('cat_final') or s.get('tags', '')}\n"
        f"script:\n{s.get('script', '')}"
        for s in scripts
    )
    known_block = _format_known_fields(known_fields)
    product_details = (known_fields.get("product_details") or "").strip()
    product_block = (
        f"\n\nPRODUCT / ITEM DETAILS (raw text pulled from the Ecom Record, Sales "
        f"Order, or Cash Sale's line item in NetSuite — whatever the customer is "
        f"asking about, like specs, dimensions, thread size, OEM part number, "
        f"fitment, is probably buried in here. Read it and pull out the actual "
        f"answer to fill into placeholders like _INFO_ — don't leave a spec "
        f"placeholder empty just because the raw text is messy):\n{product_details}"
        if product_details
        else ""
    )

    return f"""You are helping a customer support agent draft a reply. You are NOT
sending anything — a human reviews and copies your draft before it goes
anywhere, so it is safe to make a best-effort pick.

TICKET SUBJECT:
{ticket.get('subject', '')}

TICKET MESSAGE:
{ticket.get('body_text', '')}

KNOWN FACTS (ground truth — already looked up in NetSuite/eDesk, do not
contradict these, and use them directly for any matching placeholder like
_TRACKINGNUM_ or _CARRIER_):
{known_block or '(none — this ticket may not be about a specific order/shipment at all; judge purely from the ticket text)'}
{product_block}

CANDIDATE REPLY TEMPLATES (pick exactly one by id as script_id):
{scripts_block}

Pick the template that best answers what the customer is actually asking.
For its placeholder tokens (things like _CLAIMTYPE_, _DELSTATUS_, _PLATFORM_,
_INFO_), fill in everything you can confidently work out from the ticket
text, the known facts, or the product/item details above — the goal is a
fully drafted reply, not a fill-in-the-blanks template, so make a genuine
effort on every token before leaving one out. Only omit a token from
placeholder_values when you truly have nothing to go on for it."""


def draft_reply(ticket: dict, known_fields: dict, scripts: list[dict], log=print) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to a .env file next to this app "
            "(see .env.example)."
        )

    prompt = _build_prompt(ticket, known_fields, scripts)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }

    log(f"Asking Gemini ({GEMINI_MODEL}) to match a template...")
    resp = requests.post(
        GEMINI_URL,
        params={"key": api_key},
        json=body,
        timeout=45,
    )
    if not resp.ok:
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Unexpected Gemini response shape: {data}") from e

    result["placeholder_values"] = {
        item["token"]: item["value"] for item in result.get("placeholder_values", [])
    }
    return result
