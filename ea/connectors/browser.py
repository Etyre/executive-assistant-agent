"""Headless web browsing for the agent: a real Chromium (Playwright) so pages that need JavaScript work.

One browser per *context* (the main agent, and each background worker), started on first use; one "current
page" per context that the tools act on. Pages are read as text (title, condensed body text, the links on it),
paged for long documents. Search goes through DuckDuckGo's HTML results page. Nothing is persisted between runs
(no cookies, no logins).

Threads: Playwright's sync objects may only be touched from the thread that created them, and tool calls arrive
on whatever thread the engine runs them on (a pool thread under the Agent SDK, the caller's thread under the API
engine). So every context owns a thread of its own that does all its Playwright work; calls are handed to it
and the result waited for. Which context a call belongs to comes from the calling thread's `ea_context`
attribute (set by the worker pool on worker threads and by the SDK engine on its tool-dispatch threads); without
it, the call is the main agent's. A browser whose Chromium has died is relaunched on the next call."""
from __future__ import annotations

import logging
import queue
import re
import threading
import urllib.parse
from typing import Any, Callable

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


def current_context() -> str:
    return getattr(threading.current_thread(), "ea_context", "") or "main"


class _Ctx:
    """One context's browser state, driven from its own thread (see the module docstring)."""
    def __init__(self, key: str) -> None:
        self.key = key
        self.pw = None
        self.browser = None
        self.page = None
        self.last: dict[str, Any] = {}
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name=f"browser:{key}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            fn, box, done = self._q.get()
            if fn is None:
                return
            try:
                box["result"] = fn()
            except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
                box["error"] = e
            done.set()

    def run(self, fn: Callable[[], Any]) -> Any:
        """Run fn on this context's thread and return its result (or raise what it raised)."""
        if threading.current_thread() is self._thread:
            return fn()
        box: dict[str, Any] = {}
        done = threading.Event()
        self._q.put((fn, box, done))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]

    def alive(self) -> bool:
        try:
            return self.page is not None and not self.page.is_closed() and self.browser.is_connected()
        except Exception:  # noqa: BLE001
            return False

    def discard(self) -> None:
        """Close what is open (quietly: Chromium may already be gone). Runs on the context's thread."""
        for closer in ((lambda: self.browser.close()) if self.browser else None, (lambda: self.pw.stop()) if self.pw else None):
            try:
                if closer:
                    closer()
            except Exception:  # noqa: BLE001
                pass
        self.pw = self.browser = self.page = None
        self.last = {}

    def stop(self) -> None:
        try:
            self.run(self.discard)
        except Exception:  # noqa: BLE001
            pass
        self._q.put((None, None, None))


class Browser:
    def __init__(self, timeout_ms: int = 25_000, max_chars: int = 12_000):
        self.timeout = timeout_ms
        self.max_chars = max_chars
        self._ctxs: dict[str, _Ctx] = {}
        self._ctx_lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def _ctx(self) -> _Ctx:
        key = current_context()
        with self._ctx_lock:
            c = self._ctxs.get(key)
            if c is None:
                c = self._ctxs[key] = _Ctx(key)
            return c

    def _run(self, fn: Callable[[_Ctx], Any]) -> Any:
        """Run fn(ctx) on the calling context's browser thread."""
        c = self._ctx()
        return c.run(lambda: fn(c))

    def _ensure(self, c: _Ctx):
        """The context's page, launching Chromium on first use and again if it died. Runs on the context's thread."""
        if c.alive():
            return c.page
        if c.browser is not None or c.pw is not None:
            log.warning("browser[%s]: Chromium is gone; relaunching", c.key)
            c.discard()
        from playwright.sync_api import sync_playwright
        c.pw = sync_playwright().start()
        c.browser = c.pw.chromium.launch(headless=True)
        ctx = c.browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900}, locale="en-US")
        ctx.set_default_timeout(self.timeout)
        c.page = ctx.new_page()
        return c.page

    def contexts(self) -> dict[str, bool]:
        """Context key -> whether its Chromium is up (for status reports)."""
        with self._ctx_lock:
            return {k: c.alive() for k, c in self._ctxs.items()}

    def close_context(self, key: str) -> None:
        """Shut one context's browser and thread (a worker that finished)."""
        with self._ctx_lock:
            c = self._ctxs.pop(key, None)
        if c is not None:
            c.stop()

    def close(self) -> None:
        with self._ctx_lock:
            ctxs = list(self._ctxs.values())
            self._ctxs.clear()
        for c in ctxs:
            c.stop()

    # ------------------------------------------------------------------ reading (all run on the context's thread)
    def _read(self, c: _Ctx, offset: int = 0, max_chars: int | None = None) -> dict[str, Any]:
        page = self._ensure(c)
        data = page.evaluate(TEXT_JS)
        text = _condense(data.get("text") or "")
        c.last = {"url": data.get("url"), "title": data.get("title"), "text": text, "links": data.get("links") or [], "inputs": data.get("inputs") or []}
        return self._slice(c, offset, max_chars)

    def _slice(self, c: _Ctx, offset: int, max_chars: int | None) -> dict[str, Any]:
        n = max_chars or self.max_chars
        text = c.last.get("text", "")
        chunk = text[offset:offset + n]
        out = {"url": c.last.get("url"), "title": c.last.get("title"), "chars": len(text), "offset": offset, "text": chunk}
        if offset + n < len(text):
            out["next_offset"] = offset + n
        links = c.last.get("links", [])
        if offset == 0:
            out["links"] = links[:60]
            if len(links) > 60:
                out["more_links"] = len(links) - 60
            if c.last.get("inputs"):
                out["form_fields"] = c.last["inputs"][:20]
        return out

    def open(self, url: str, max_chars: int | None = None) -> dict[str, Any]:
        if not re.match(r"^https?://", url):
            url = "https://" + url
        return self._run(lambda c: self._open(c, url, max_chars))

    def _open(self, c: _Ctx, url: str, max_chars: int | None) -> dict[str, Any]:
        page = self._ensure(c)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception as e:  # noqa: BLE001 - slow pages: read what we have
            if not c.alive():                   # Chromium died under us: once more on a fresh one
                log.warning("browser[%s]: %s loading %s and Chromium is gone; relaunching", c.key, type(e).__name__, url)
                self._ensure(c).goto(url, wait_until="domcontentloaded")
            else:
                log.info("browser: %s while loading %s; reading anyway", type(e).__name__, url)
        return self._read(c, 0, max_chars)

    def more(self, offset: int, max_chars: int | None = None) -> dict[str, Any]:
        def go(c: _Ctx):
            if not c.last:
                return {"error": "no page open; use web_open first"}
            return self._slice(c, offset, max_chars)
        return self._run(go)

    def links(self, contains: str = "", limit: int = 100) -> list[dict[str, Any]]:
        def go(c: _Ctx):
            ls = c.last.get("links", [])
            if contains:
                cc = contains.lower()
                ls = [l for l in ls if cc in l["text"].lower() or cc in l["href"].lower()]
            return ls[:limit]
        return self._run(go)

    def click(self, link_text: str, max_chars: int | None = None) -> dict[str, Any]:
        """Follow a link by its visible text (first match, case-insensitive; or an exact href)."""
        def go(c: _Ctx):
            page = self._ensure(c)
            target = None
            lt = link_text.strip().lower()
            for l in c.last.get("links", []):
                if l["href"] == link_text or l["text"].lower() == lt:
                    target = l; break
            if target is None:
                for l in c.last.get("links", []):
                    if lt in l["text"].lower():
                        target = l; break
            if target is None:
                try:
                    page.get_by_role("link", name=link_text, exact=False).first.click()
                    page.wait_for_load_state("domcontentloaded")
                    return self._read(c, 0, max_chars)
                except Exception as e:  # noqa: BLE001
                    return {"error": f"no link matching {link_text!r} on this page ({type(e).__name__})"}
            return self._open(c, target["href"], max_chars)
        return self._run(go)

    def fill(self, field: str, value: str, submit: bool = False, max_chars: int | None = None) -> dict[str, Any]:
        """Type into a form field found by label, placeholder, name or id; optionally press Enter."""
        def go(c: _Ctx):
            page = self._ensure(c)
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
            return self._read(c, 0, max_chars)
        return self._run(go)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        def go(c: _Ctx):
            page = self._ensure(c)
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
        return self._run(go)
