"""EDNIS Drafter — a standalone companion to EDNIS.

Reads the open eDesk ticket + the real NetSuite tracking info, asks Gemini to
pick the best-matching Fetchbox reply template and fill in what it can, and
shows the result in this window for review. Nothing is ever sent or pasted
into eDesk automatically — the user copies the reviewed draft themselves.

Connects to the same automation Chrome window EDNIS uses (start it with
ednis/launch_chrome_debug.bat first). Does not import from or modify
anything under ednis/ — see edesk_reader.py / netsuite_reader.py for the
(separately maintained) replicated connection logic.
"""

import json
import os
import threading

import webview
from dotenv import load_dotenv

load_dotenv()

import edesk_composer
import fetchbox_bridge
import gemini_client
from edesk_reader import read_ticket
from netsuite_reader import read_product_details, read_tracking_numbers
from tracking_parser import detect_carrier

# Fetchbox's repeating-shipment blocks (_1TRACKINGNUM_, _2TRACKINGNUM_, ...)
# only go up to this many — matches _NUMSHIPMENTS_'s options in
# fetchbox_bridge.SELECT_FIELDS.
MAX_SHIPMENTS = 4

HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")


class Api:
    """Exposed to the page as `window.pywebview.api`."""

    def __init__(self):
        self._window = None
        self._pick_event = None
        self._pick_result = None
        self._last_scripts_by_id = {}
        self._last_known_values = {}
        self._last_ticket_url = None
        self._last_name = ""

    def _bind(self, window):
        self._window = window

    # --- Python -> page ------------------------------------------------

    def _js(self, fn, *args):
        payload = ", ".join(json.dumps(a) for a in args)
        self._window.evaluate_js(f"window.app.{fn}({payload})")

    def _status(self, text, state="info"):
        self._js("setStatus", text, state)

    def _busy(self, is_busy):
        self._js("setBusy", is_busy)

    def _pick_page(self, labels):
        """Blocks the worker thread until the user picks a ticket in the
        page's overlay (or cancels)."""
        self._pick_event = threading.Event()
        self._pick_result = None
        self._js("showPicker", labels)
        self._pick_event.wait()
        if self._pick_result is None:
            raise RuntimeError("Cancelled — no ticket selected.")
        return self._pick_result

    # --- page -> Python -------------------------------------------------

    def resolve_pick(self, index):
        self._pick_result = index
        if self._pick_event:
            self._pick_event.set()

    def minimize(self):
        self._window.minimize()

    def close(self):
        self._window.destroy()

    def fit(self, width, height):
        try:
            self._window.resize(max(280, int(round(width))), max(200, int(round(height))))
        except Exception:
            pass

    def set_name(self, name):
        self._last_name = (name or "").strip()

    def analyze_ticket(self):
        threading.Thread(target=self._run_analyze, daemon=True).start()

    def switch_alternate(self, script_id):
        threading.Thread(target=self._run_switch, args=(script_id,), daemon=True).start()

    def insert_into_edesk(self, draft_text):
        threading.Thread(target=self._run_insert, args=(draft_text,), daemon=True).start()

    # --- workers ---------------------------------------------------------

    def _run_analyze(self):
        """Reads the ticket first, then decides what NetSuite lookups (if
        any) are actually relevant — same as a rep would: not every ticket
        is about an order/shipment, so a missing order # is not an error,
        it's just one less thing to look up before drafting a reply."""
        self._busy(True)
        try:
            self._status("Reading the ticket...")
            ticket = read_ticket(log=self._status, pick_page=self._pick_page)
            self._last_ticket_url = ticket.get("url")

            shipments = []
            if ticket.get("order_query"):
                self._status("Order number found — looking up tracking in NetSuite...")
                try:
                    tracking_numbers = read_tracking_numbers(ticket["order_query"], log=self._status)
                    shipments = [
                        {"tracking_number": num, "carrier": detect_carrier(num)}
                        for num in tracking_numbers
                    ]
                except Exception as e:
                    # A NetSuite hiccup shouldn't block drafting a reply —
                    # the ticket text + Fetchbox alone can still produce
                    # something useful; just note tracking wasn't confirmed.
                    self._status(f"Couldn't confirm NetSuite tracking ({e}) — drafting without it.")
            else:
                self._status("No order number on this ticket — drafting from the ticket text alone.")

            product_details = ""
            if ticket.get("ecom_number") or ticket.get("order_query"):
                self._status("Looking up product/item details in NetSuite...")
                try:
                    product_details = read_product_details(
                        order_query=ticket.get("order_query"),
                        ecom_number=ticket.get("ecom_number"),
                        log=self._status,
                    )
                except Exception as e:
                    self._status(f"Couldn't look up product details ({e}) — drafting without them.")
                if product_details:
                    self._status(f"Got {len(product_details)} chars of product details from NetSuite.")
                else:
                    self._status("No product details came back from NetSuite for this item.")

            known_fields = {
                "order_number": ticket.get("order_query"),
                "ecom_number": ticket.get("ecom_number"),
                "shipped": bool(shipments),
                "shipments": shipments,
                "product_details": product_details,
            }

            # Single package: fill the plain _TRACKINGNUM_/_CARRIER_ tokens.
            # Multiple packages: fill Fetchbox's repeating-block tokens
            # (_1TRACKINGNUM_, _2TRACKINGNUM_, ...) and _NUMSHIPMENTS_ so
            # build_repeating_script expands the template to match — a
            # Sales Order's Tracking # field sometimes holds several numbers
            # space-separated on one line for a multi-package shipment.
            known_values = {}
            if len(shipments) == 1:
                num, carrier = shipments[0]["tracking_number"], shipments[0]["carrier"]
                known_values["_TRACKINGNUM_"] = num
                if carrier:
                    known_values["_CARRIER_"] = carrier
            elif len(shipments) > 1:
                capped = shipments[:MAX_SHIPMENTS]
                known_values["_NUMSHIPMENTS_"] = str(len(capped))
                for i, s in enumerate(capped, start=1):
                    known_values[f"_{i}TRACKINGNUM_"] = s["tracking_number"]
                    if s["carrier"]:
                        known_values[f"_{i}CARRIER_"] = s["carrier"]
            self._last_known_values = known_values

            self._status("Fetching Fetchbox templates...")
            scripts = fetchbox_bridge.fetch_scripts(log=self._status)
            self._last_scripts_by_id = {s["id"]: s for s in scripts}

            self._status("Matching against Fetchbox templates...")
            result = gemini_client.draft_reply(ticket, known_fields, scripts, log=self._status)

            self._push_result(result)
            self._status("Draft ready — review before sending. Nothing was sent.", "success")
        except Exception as e:
            self._status(str(e), "error")
        finally:
            self._busy(False)

    def _run_insert(self, draft_text):
        """Inserts whatever's currently in the draft textarea (the user may
        have edited it) into eDesk's own reply box. Never sends — the user
        reviews it in eDesk and clicks Send themselves."""
        self._busy(True)
        try:
            if not self._last_ticket_url:
                self._status("Analyze a ticket first.", "error")
                return
            inserted = edesk_composer.insert_draft(self._last_ticket_url, draft_text, log=self._status)
            if inserted:
                self._status("Inserted into eDesk — review it there, then Send yourself.", "success")
            else:
                self._status("Couldn't insert into eDesk — use Copy instead.", "error")
        except Exception as e:
            self._status(str(e), "error")
        finally:
            self._busy(False)

    def _run_switch(self, script_id):
        self._busy(True)
        try:
            script = self._last_scripts_by_id.get(script_id)
            if not script:
                self._status(
                    "That template isn't loaded anymore — analyze the ticket again.",
                    "error",
                )
                return
            draft = fetchbox_bridge.render_script(
                script.get("script", ""), self._last_known_values, self._last_name
            )
            self._js("setDraft", draft, script.get("cat1", script_id))
            self._status(f"Switched to: {script.get('cat1', script_id)}", "success")
        except Exception as e:
            self._status(str(e), "error")
        finally:
            self._busy(False)

    def _push_result(self, result):
        script_id = result.get("script_id")
        script = self._last_scripts_by_id.get(script_id)
        if not script:
            raise RuntimeError(f"Gemini picked an unknown template id: {script_id}")

        # Known (NetSuite-sourced) values always win over anything Gemini
        # guessed for the same placeholder token.
        merged_values = {**result.get("placeholder_values", {}), **self._last_known_values}
        draft = fetchbox_bridge.render_script(script.get("script", ""), merged_values, self._last_name)

        alternates = [
            {
                "id": alt.get("script_id"),
                "title": self._last_scripts_by_id.get(alt.get("script_id"), {}).get(
                    "cat1", alt.get("script_id")
                ),
                "why": alt.get("why", ""),
            }
            for alt in result.get("alternates", [])
            if alt.get("script_id") in self._last_scripts_by_id
        ]

        self._js(
            "showResult",
            result.get("summary", ""),
            draft,
            script.get("cat1", script_id),
            alternates,
        )


def main():
    api = Api()
    window = webview.create_window(
        "EDNIS Drafter",
        HTML_FILE,
        js_api=api,
        width=380,
        height=440,
        min_size=(300, 240),
        frameless=True,
        on_top=True,
        background_color="#1a1030",
    )
    api._bind(window)
    webview.start()


if __name__ == "__main__":
    main()
