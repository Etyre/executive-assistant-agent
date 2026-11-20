"""A signed-in browser for one web site, so the agent can use an account of Eli's there (built 2026-11-19 to order
food on Grubhub). It runs on the Mac, inside the bridge: Eli signs in once himself in this browser's own Chrome
profile (bridge/food_signin.py), and from then on the agent drives the site headless.

Unlike ea/connectors/browser.py (anonymous reading, nothing kept) this one keeps its profile, can press any button,
and is held to a short list of hosts. A page is shown to the model as text plus a numbered list of the things on it
that can be clicked or typed into; the numbers are valid until the page changes.

What it will not do, whatever the model asks: leave the allowed hosts; type into a password or card field; press
the button that completes a purchase (that goes through `press_final`, which the caller wraps in its own checks,
see ea/food.py). It makes no attempt to look like a person: if the site answers with a bot check, that is
reported and the task stops there.

Threads: all Playwright work happens on one thread owned by this object (Playwright's sync objects are bound to the
thread that made them, and the bridge answers requests on a pool)."""
from __future__ import annotations

import logging
import pathlib
import re
import time
import urllib.parse
from typing import Any, Callable

from .browser import _Ctx, _condense

log = logging.getLogger(__name__)

# The button that spends money. Matched against a button's whole label, so "Place order" and "Place your order: $23.10"
# match and "Order again" or "View order" do not.
FINAL = re.compile(r"^\s*(place|submit|complete|confirm)\b.{0,12}\border\b|^\s*pay\b(?! with)|^\s*buy now\b", re.I)
BOT_CHECK = re.compile(r"verify(ing)? (that )?you are (a )?human|not a (ro)?bot|security verification|unusual activity|captcha|press (and|&) hold", re.I)
IDLE_CLOSE_S = 600          # Chrome is shut after ten quiet minutes; the next call starts it again

SCAN_JS = r"""() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 1 && r.height > 1 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const dialogs = [...document.querySelectorAll('[role=dialog], [aria-modal=true], dialog[open]')].filter(vis);
  const root = dialogs.length ? dialogs[dialogs.length - 1] : document.body;
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','IFRAME']);
  const walk = (n) => { if (!n) return ''; if (n.nodeType === 3) return n.textContent; if (n.nodeType !== 1 || skip.has(n.tagName)) return '';
    const cs = getComputedStyle(n); if (cs.display === 'none' || cs.visibility === 'hidden') return '';
    let out = ''; for (const c of n.childNodes) out += walk(c);
    if (n.tagName === 'INPUT' && n.type !== 'hidden' && n.type !== 'password' && n.value && !/^(checkbox|radio)$/.test(n.type)) out += ' [' + n.value + '] ';
    return /^(block|list-item|table|table-row|flex|grid)$/.test(cs.display) ? '\n' + out + '\n' : out; };
  document.querySelectorAll('[data-atlas-ref]').forEach(e => e.removeAttribute('data-atlas-ref'));
  const sel = 'button, a[href], [role=button], [role=link], [role=option], [role=radio], [role=checkbox], [role=tab], [role=menuitem], [role=switch], input, select, textarea, label[for]';
  const els = []; let n = 0;
  for (const e of root.querySelectorAll(sel)) {
    if (e.type === 'hidden' || !vis(e)) continue;
    const tag = e.tagName.toLowerCase();
    let label = (e.innerText || '').trim() || e.getAttribute('aria-label') || e.getAttribute('title') || '';
    if (/^(input|select|textarea)$/.test(tag)) {
      const lab = e.labels && e.labels[0] ? e.labels[0].innerText : '';
      label = (lab || e.getAttribute('aria-label') || e.placeholder || e.name || e.id || '').trim();
    }
    label = label.replace(/\s+/g, ' ').slice(0, 140);
    if (!label && tag !== 'input') continue;
    const parent = e.parentElement && e.parentElement.closest('[data-atlas-ref]');
    if (parent && parent.dataset.atlasLabel === label) continue;      // a button inside a link saying the same thing
    n += 1; e.setAttribute('data-atlas-ref', String(n)); e.dataset.atlasLabel = label;
    const kind = tag === 'a' ? 'link' : /^(input|select|textarea)$/.test(tag) ? (e.type || tag) : (e.getAttribute('role') || 'button');
    const item = {n, kind, label};
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') item.disabled = true;
    if (e.checked || e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-selected') === 'true') item.on = true;
    if (tag === 'select') item.options = [...e.options].map(o => o.text.trim()).slice(0, 40);
    if (/^(input|textarea)$/.test(tag) && e.value && e.type !== 'password' && !/^(checkbox|radio)$/.test(e.type)) item.value = e.value.slice(0, 80);
    els.push(item);
  }
  return {url: location.href, title: document.title, dialog: dialogs.length > 0, text: walk(root), elements: els};
}"""

# A field the agent may never type into: passwords, and anything a browser would call card or bank details.
SECRET_FIELD_JS = r"""(e) => { const a = ((e.getAttribute('autocomplete') || '') + ' ' + (e.name || '') + ' ' + (e.id || '') + ' ' +
  (e.getAttribute('aria-label') || '') + ' ' + (e.placeholder || '')).toLowerCase();
  return e.type === 'password' || /cc-|card.?num|cardnumber|cvv|cvc|security.?code|routing|account.?num|ssn|password/.test(a); }"""


class SiteError(Exception):
    """Something the model should read and act on (an unknown number, a refused action)."""


class SiteBrowser:
    def __init__(self, profile: pathlib.Path, hosts: list[str], home: str, timeout_ms: int = 30_000, headless: bool = True):
        self.profile = pathlib.Path(profile).expanduser()
        self.hosts = [h.lower().lstrip(".") for h in hosts]
        self.home = home
        self.timeout = timeout_ms
        self.headless = headless
        self._c = _Ctx("site")
        self._context = None
        self._last_use = 0.0

    # ------------------------------------------------------------------ lifecycle (context thread)
    def allowed(self, url: str) -> bool:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def _page(self):
        c = self._c
        if self._context is not None and time.time() - self._last_use > IDLE_CLOSE_S:
            self._shut()
        self._last_use = time.time()
        try:
            if c.page is not None and not c.page.is_closed():
                return c.page
        except Exception:  # noqa: BLE001 - Chrome went away: start again
            pass
        self._shut()
        from playwright.sync_api import sync_playwright
        self.profile.mkdir(parents=True, exist_ok=True)
        c.pw = sync_playwright().start()
        # Eli's installed Chrome with a profile of its own (never his everyday one), so the sign-in survives.
        self._context = c.pw.chromium.launch_persistent_context(
            str(self.profile), channel="chrome", headless=self.headless, viewport={"width": 1280, "height": 900}, locale="en-US")
        self._context.set_default_timeout(self.timeout)
        c.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return c.page

    def _shut(self) -> None:
        c = self._c
        for closer in (lambda: self._context.close(), lambda: c.pw.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001 - already gone
                pass
        self._context = c.pw = c.page = None

    def close(self) -> dict[str, Any]:
        """Shut Chrome (the profile stays). The sign-in window needs this: one Chrome per profile at a time."""
        self._c.run(self._shut)
        return {"closed": True}

    def _run(self, fn: Callable[[], Any]) -> Any:
        return self._c.run(fn)

    # ------------------------------------------------------------------ reading
    def _settle(self, page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=self.timeout)
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:  # noqa: BLE001 - a busy page never goes idle: read what is there
            pass
        page.wait_for_timeout(400)
        if not self.allowed(page.url) and page.url not in ("about:blank", ""):
            away = page.url
            try:
                page.go_back(wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                page.goto(self.home, wait_until="domcontentloaded")
            raise SiteError(f"that led off the site to {away.split('?')[0]}, which this browser is not allowed to visit; went back")

    def _scan(self, page) -> dict[str, Any]:
        data = page.evaluate(SCAN_JS)
        data["text"] = _condense(data.get("text") or "")
        return data

    def _view(self, find: str = "", offset: int = 0, max_chars: int = 6000, limit: int = 80) -> dict[str, Any]:
        page = self._page()
        if page.url in ("about:blank", ""):
            page.goto(self.home, wait_until="domcontentloaded")
            self._settle(page)
        data = self._scan(page)
        text, els = data["text"], data["elements"]
        out: dict[str, Any] = {"url": data["url"], "title": data["title"]}
        if BOT_CHECK.search(text[:3000]) and len(els) < 15:
            out["blocked"] = ("The site is showing a bot check. Do not try to get past it: stop, and tell Eli that the "
                              "site refused the automated browser.")
        if data["dialog"]:
            out["dialog_open"] = "a dialog is open; the text and elements below are the dialog's (close it to reach the page behind)"
        out["text"] = text[offset:offset + max_chars]
        if offset + max_chars < len(text):
            out["next_offset"] = offset + max_chars
        if find:
            f = find.lower()
            els = [e for e in els if f in e["label"].lower()]
        out["elements"] = els[:limit]
        if len(els) > limit:
            out["more_elements"] = f"{len(els) - limit} more: narrow with `find`"
        return out

    def view(self, find: str = "", offset: int = 0) -> dict[str, Any]:
        return self._run(lambda: self._view(find, offset))

    def open(self, url: str) -> dict[str, Any]:
        if not re.match(r"^https?://", url):
            url = "https://" + url
        if not self.allowed(url):
            raise SiteError(f"this browser only visits {', '.join(self.hosts)}")

        def go():
            page = self._page()
            page.goto(url, wait_until="domcontentloaded")
            self._settle(page)
            return self._view()
        return self._run(go)

    # ------------------------------------------------------------------ acting
    def _el(self, page, n: int):
        loc = page.locator(f'[data-atlas-ref="{int(n)}"]')
        if loc.count() != 1:
            raise SiteError(f"there is no element {n} on the page as it is now; look at the page again for current numbers")
        return loc.first

    def click(self, n: int) -> dict[str, Any]:
        def go():
            page = self._page()
            el = self._el(page, n)
            label = el.get_attribute("data-atlas-label") or ""
            if FINAL.search(label):
                raise SiteError(f"{label!r} completes a purchase, which this tool never presses; use the order-placing tool")
            el.click()
            self._settle(page)
            return self._view()
        return self._run(go)

    def type(self, n: int, text: str, enter: bool = False) -> dict[str, Any]:
        def go():
            page = self._page()
            el = self._el(page, n)
            if el.evaluate(SECRET_FIELD_JS):
                raise SiteError("that is a password or payment field; the agent never types into those. Eli enters them himself.")
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            if tag == "select":
                el.select_option(label=text)
            else:
                el.fill("")
                el.type(text, delay=20)             # typed, not pasted: address boxes only suggest while keys arrive
            if enter:
                el.press("Enter")
            self._settle(page)
            return self._view()
        return self._run(go)

    def scroll(self, pages: float = 1.0) -> dict[str, Any]:
        """Scroll down (or up, negative) so that a long list loads more; then the page as it is."""
        def go():
            page = self._page()
            page.mouse.wheel(0, int(800 * pages))
            page.wait_for_timeout(900)
            return self._view()
        return self._run(go)

    def back(self) -> dict[str, Any]:
        def go():
            page = self._page()
            page.go_back(wait_until="domcontentloaded")
            self._settle(page)
            return self._view()
        return self._run(go)

    def screenshot(self, path: pathlib.Path) -> None:
        self._run(lambda: self._page().screenshot(path=str(path), full_page=True))

    # ------------------------------------------------------------------ the purchase button
    def final_state(self) -> dict[str, Any]:
        """The page's text and its purchase buttons, for the caller's checks before `press_final`."""
        def go():
            page = self._page()
            data = self._scan(page)
            return {"url": data["url"], "title": data["title"], "text": data["text"],
                    "buttons": [e for e in data["elements"] if FINAL.search(e["label"]) and not e.get("disabled")]}
        return self._run(go)

    def press_final(self, n: int) -> dict[str, Any]:
        """Press purchase button `n` (from final_state, with no look at the page in between) and report what the
        site shows afterwards. The caller has already decided this purchase is allowed."""
        def go():
            page = self._page()
            el = self._el(page, n)
            if not FINAL.search(el.get_attribute("data-atlas-label") or ""):
                raise SiteError("that is not a purchase button")
            el.click()
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(1500)
            if not self.allowed(page.url):              # e.g. the bank's own confirmation page: Eli's to deal with
                return {"url": page.url.split("?")[0], "title": page.title(), "text": "", "buttons": [], "left_site": True}
            data = self._scan(page)
            return {"url": data["url"], "title": data["title"], "text": data["text"],
                    "buttons": [e for e in data["elements"] if FINAL.search(e["label"])]}
        return self._run(go)
