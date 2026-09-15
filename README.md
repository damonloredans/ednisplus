# EDNIS Drafter

A standalone companion to [EDNIS](../ednis) — reads the eDesk ticket you have
open, looks up the real tracking info in NetSuite, asks Gemini to pick the
best-matching reply from your Fetchbox templates and fill in what it can,
and shows you a summary + editable draft to review.

**It never sends or pastes anything into eDesk.** The draft is copy-only —
you review it, edit it if needed, hit Copy, and paste it in yourself.

This is a separate tool from EDNIS: it doesn't modify or depend on any file
in `ednis/`, it just connects to the same automation Chrome window.

## How it fits together

- **eDesk ticket** → read via Playwright/CDP, same automation Chrome window
  EDNIS uses (`edesk_reader.py`).
- **Tracking number** → read from the Sales Order's **Shipping** subtab in
  NetSuite (`netsuite_reader.py`), *not* from eDesk — the two have been seen
  to disagree, so NetSuite is the source of truth. Carrier is then detected
  from the tracking number's own format (`tracking_parser.py`).
- **Reply templates** → pulled straight from the public Google Sheet behind
  Fetchbox (`fetchbox_bridge.py`), same one `quico/server` proxies — no
  Fetchbox login needed.
- **Matching + drafting** → a single Gemini call (`gemini_client.py`, free
  API tier) picks the best template and infers what placeholders it safely
  can; anything it's not sure about is left for you to fill in by hand.

## Setup

1. `install.bat` — creates a virtualenv, installs dependencies, copies
   `.env.example` to `.env`.
2. Add your free Gemini API key (from [aistudio.google.com](https://aistudio.google.com/apikey))
   to `.env`.
3. Fill in the real per-carrier tracking-number patterns in
   `tracking_parser.py` (`CARRIER_TRACKING_PATTERNS`) — only a couple of
   examples are stubbed in.
4. Run `ednis\launch_chrome_debug.bat` first (shared automation Chrome
   window), log into NetSuite + eDesk.
5. `run.bat` to start EDNIS Drafter.

## Known rough edges to expect on first real use

- `edesk_reader.py`'s subject/message-body reader and `netsuite_reader.py`'s
  Shipping-subtab/Tracking-# reader both use best-effort selectors that
  haven't been verified against a live ticket/order yet — expect to need to
  adjust them once tested for real.
- `tracking_parser.CARRIER_TRACKING_PATTERNS` needs the real carrier
  patterns filled in.
