"""Headless browsing tools (connectors/browser.py)."""
from __future__ import annotations

from typing import Any

from ..connectors.browser import Browser
from .registry import ToolRegistry


def register(reg: ToolRegistry, browser: Browser) -> None:
    def guard(fn):
        def run(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001 - the model gets the reason
                return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        return run

    @reg.tool("web_search", "Search the web (DuckDuckGo). Returns titles, URLs and snippets; open a result with web_open.",
              {"properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"]})
    @guard
    def web_search(query: str, limit: int = 8) -> Any:
        return browser.search(query, limit=limit)

    @reg.tool("web_open", "Open a web page in the headless browser (JavaScript runs) and read it as text: title, the main "
              "text (paged; pass next_offset to web_more for the rest), the links on it (follow one with web_click), and "
              "any form fields (fill one with web_fill).",
              {"properties": {"url": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 1000, "maximum": 40000}}, "required": ["url"]})
    @guard
    def web_open(url: str, max_chars: int | None = None) -> Any:
        return browser.open(url, max_chars=max_chars)

    @reg.tool("web_more", "The next part of the current page's text, from `offset` (the next_offset of the previous read).",
              {"properties": {"offset": {"type": "integer", "minimum": 0}, "max_chars": {"type": "integer", "minimum": 1000, "maximum": 40000}}, "required": ["offset"]})
    @guard
    def web_more(offset: int, max_chars: int | None = None) -> Any:
        return browser.more(offset, max_chars=max_chars)

    @reg.tool("web_links", "Links on the current page, optionally only those whose text or URL contains `contains`.",
              {"properties": {"contains": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 300}}})
    @guard
    def web_links(contains: str = "", limit: int = 100) -> Any:
        return browser.links(contains, limit=limit)

    @reg.tool("web_click", "Follow a link on the current page by its visible text (or exact URL) and read the new page.",
              {"properties": {"link_text": {"type": "string"}}, "required": ["link_text"]})
    @guard
    def web_click(link_text: str) -> Any:
        return browser.click(link_text)

    @reg.tool("web_fill", "Type into a form field on the current page (found by its label, placeholder, name or id), and "
              "optionally submit with Enter; then read the resulting page. For searches on a site, login-free forms. "
              "Never enter the user's passwords or payment details.",
              {"properties": {"field": {"type": "string"}, "value": {"type": "string"}, "submit": {"type": "boolean"}}, "required": ["field", "value"]}, writes=True)
    @guard
    def web_fill(field: str, value: str, submit: bool = False) -> Any:
        return browser.fill(field, value, submit=submit)
