"""Shared helpers for finding the right tab and opening new tabs without
stealing focus from whatever tab the user is currently looking at — the same
effect as Ctrl+Click or middle-click on a link.

Replicated from ../ednis/chrome_utils.py (kept as a separate copy rather than
an import so this tool has no dependency on the ednis/ folder layout).
"""


def find_active_page(context, domain: str):
    """Finds the page whose URL contains `domain`. If several tabs match,
    prefers whichever one is actually visible/focused right now instead of
    just the first one found."""
    candidates = [p for p in context.pages if domain in p.url]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    for p in candidates:
        try:
            if p.evaluate("document.visibilityState") == "visible":
                return p
        except Exception:
            continue
    return candidates[0]


def open_background_tab(context, url: str, timeout: float = 9000):
    """Opens `url` in a new tab without switching focus to it. Returns the
    Playwright Page for the new tab."""
    try:
        with context.expect_page(timeout=timeout) as new_page_info:
            cdp = context.new_cdp_session(context.pages[0])
            cdp.send("Target.createTarget", {"url": url, "background": True})
        return new_page_info.value
    except Exception as e:
        raise RuntimeError(f"Timed out waiting for new background tab: {url}") from e
