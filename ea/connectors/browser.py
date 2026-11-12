"""Headless web browsing for the agent: a real Chromium (Playwright) so pages that need JavaScript work.

One browser per server process, started on first use; one "current page" the tools act on. Pages are read as
text (title, condensed body text, the links on it), paged for long documents. Search goes through DuckDuckGo's
HTML results page. Nothing is persisted between runs (no cookies, no logins)."""
from __future__ import annotations

import logging
import re
import threading
import urllib.parse
from typing import Any

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TEXT_JS = """() => {
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','IFRAME']);
  const main = document.querySelector('main, article, [role=main]') || document.body;
  const walk = (n) => { if (!n) return ''; if (n.nodeType === 3) return n.textContent; if (skip.has(n.tagName)) return '';
    const cs = n.nodeType === 1 ? getComputedStyle(n) : null; if (cs && (cs.display === 'none' || cs.visibility === 'hidden')) return '';
    let out = ''; for (const c of n.childNodes) out += walk(c);
    const block = cs && /^(block|list-item|table|table-row|flex|grid)$/.test(cs.display);
    return block ? '\\n' + out + '\\n' : out; };
  const links = [...document.querySelectorAll('a[href]')].filter(a => a.innerText.trim() && a.href.startsWith('http'))
    .slice(0, 400).map(a => ({text: a.innerText.trim().replace(/\\s+/g, ' ').slice(0, 120), href: a.href}));
  const inputs = [...document.querySelectorAll('input, textarea, select')].filter(i => i.type !== 'hidden').slice(0, 40)
    .map(i => ({tag: i.tagName.toLowerCase(), type: i.type || '', name: i.name || '', id: i.id || '', placeholder: i.placeholder || '', label: (i.labels && i.labels[0] ? i.labels[0].innerText : '').trim().slice(0, 60)}));
  return {title: document.title, text: walk(main), links, inputs, url: location.href};
}"""


def _condense(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _Ctx:
    """One browser context's state: the main agent's, or a worker's (Playwright's sync objects are bound to the
    thread that made them, so each worker thread gets its own)."""
    def __init__(self) -> None:
        self.pw = None
        self.browser = None
        self.page = None
        self.last: dict[str, Any] = {}
        self.lock = threading.RLock()


class Browser:
    def __init__(self, timeout_ms: int = 25_000, max_chars: int = 12_000):
        self.timeout = timeout_ms
        self.max_chars = max_chars
        self._ctxs: dict[str, _Ctx] = {}
        self._ctx_lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def _ctx(self) -> _Ctx:
        key = getattr(threading.current_thread(), "ea_context", "main")
        with self._ctx_lock:
            if key not in self._ctxs:
                self._ctxs[key] = _Ctx()
            return self._ctxs[key]

    @property
    def _lock(self) -> threading.RLock:
        return self._ctx().lock

    @property
    def _last(self) -> dict[str, Any]:
        return self._ctx().last

    @_last.setter
    def _last(self, value: dict[str, Any]) -> None:
        self._ctx().last = value

    def _ensure(self):
        c = self._ctx()
        if c.page is not None:
            return c.page
        from playwright.sync_api import sync_playwright
        c.pw = sync_playwright().start()
        c.browser = c.pw.chromium.launch(headless=True)
        ctx = c.browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}, locale="en-US")
        ctx.set_default_timeout(self.timeout)
        c.page = ctx.new_page()
        return c.page

    def close(self) -> None:
        with self._ctx_lock:
            ctxs = list(self._ctxs.values())
            self._ctxs.clear()
        for c in ctxs:
            try:
                if c.browser:
                    c.browser.close()
                if c.pw:
                    c.pw.stop()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ reading
    def _read(self, offset: int = 0, max_chars: int | None = None) -> dict[str, Any]:
        page = self._ensure()
        data = page.evaluate(TEXT_JS)
        text = _condense(data.get("text") or "")
        self._last = {"url": data.get("url"), "title": data.get("title"), "text": text, "links": data.get("links") or [], "inputs": data.get("inputs") or []}
        return self._slice(offset, max_chars)

    def _slice(self, offset: int, max_chars: int | None) -> dict[str, Any]:
        n = max_chars or self.max_chars
        text = self._last.get("text", "")
        chunk = text[offset:offset + n]
        out = {"url": self._last.get("url"), "title": self._last.get("title"), "chars": len(text), "offset": offset, "text": chunk}
        if offset + n < len(text):
            out["next_offset"] = offset + n
        links = self._last.get("links", [])
        if offset == 0:
            out["links"] = links[:60]
            if len(links) > 60:
                out["more_links"] = len(links) - 60
            if self._last.get("inputs"):
                out["form_fields"] = self._last["inputs"][:20]
        return out

    def open(self, url: str, max_chars: int | None = None) -> dict[str, Any]:
        if not re.match(r"^https?://", url):
            url = "https://" + url
        with self._lock:
            page = self._ensure()
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception as e:  # noqa: BLE001 - slow pages: read what we have
                log.info("browser: %s while loading %s; reading anyway", type(e).__name__, url)
            return self._read(0, max_chars)

    def more(self, offset: int, max_chars: int | None = None) -> dict[str, Any]:
        with self._lock:
            if not self._last:
                return {"error": "no page open; use web_open first"}
            return self._slice(offset, max_chars)

    def links(self, contains: str = "", limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            ls = self._last.get("links", [])
            if contains:
                c = contains.lower()
                ls = [l for l in ls if c in l["text"].lower() or c in l["href"].lower()]
            return ls[:limit]

    def click(self, link_text: str, max_chars: int | None = None) -> dict[str, Any]:
        """Follow a link by its visible text (first match, case-insensitive; or an exact href)."""
        with self._lock:
            page = self._ensure()
            target = None
            lt = link_text.strip().lower()
            for l in self._last.get("links", []):
                if l["href"] == link_text or l["text"].lower() == lt:
                    target = l; break
            if target is None:
                for l in self._last.get("links", []):
                    if lt in l["text"].lower():
                        target = l; break
            if target is None:
                try:
                    page.get_by_role("link", name=link_text, exact=False).first.click()
                    page.wait_for_load_state("domcontentloaded")
                    return self._read(0, max_chars)
                except Exception as e:  # noqa: BLE001
                    return {"error": f"no link matching {link_text!r} on this page ({type(e).__name__})"}
            return self.open(target["href"], max_chars)

    def fill(self, field: str, value: str, submit: bool = False, max_chars: int | None = None) -> dict[str, Any]:
        """Type into a form field found by label, placeholder, name or id; optionally press Enter."""
        with self._lock:
            page = self._ensure()
            loc = None
            for finder in (lambda: page.get_by_label(field, exact=False), lambda: page.get_by_placeholder(field, exact=False),
                           lambda: page.locator(f"[name='{field}']"), lambda: page.locator(f"#{field}")):
                try:
                    cand = finder()
                    if cand.count() > 0:
                        loc = cand.first; break
                except Exception:  # noqa: BLE001
                    continue
            if loc is None:
                return {"error": f"no field matching {field!r}; see form_fields from web_open"}
            loc.fill(value)
            if submit:
                loc.press("Enter")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
            return self._read(0, max_chars)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            page = self._ensure()
            page.goto("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query), wait_until="domcontentloaded")
            rows = page.evaluate("""() => [...document.querySelectorAll('.result')].map(r => ({
                title: (r.querySelector('.result__a') || {}).innerText || '',
                href: (r.querySelector('.result__a') || {}).href || '',
                snippet: (r.querySelector('.result__snippet') || {}).innerText || ''}))""")
            out = []
            for r in rows:
                href = r.get("href") or ""
                m = re.search(r"[?&]uddg=([^&]+)", href)
                if m:
                    href = urllib.parse.unquote(m.group(1))
                if href and r.get("title"):
                    out.append({"title": r["title"].strip(), "url": href, "snippet": (r.get("snippet") or "").strip()[:300]})
                if len(out) >= limit:
                    break
            return out
