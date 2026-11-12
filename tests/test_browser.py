"""Headless browsing over a local page (skipped if Playwright's Chromium is not installed)."""
import http.server
import pathlib
import socketserver
import tempfile
import threading

import pytest

from ea.connectors.browser import Browser, _condense

HTML = """<html><head><title>Test Page</title></head><body>
<nav><a href="/other.html">Other page</a> <a href="https://example.com/x">Example</a></nav>
<main><h1>Hello</h1><p>First paragraph with   spaces.</p><script>document.querySelector('main').appendChild(Object.assign(document.createElement('p'),{textContent:'Made by JS'}))</script>
<form><label for="q">Search box</label><input id="q" name="q" placeholder="type here"></form></main></body></html>"""
OTHER = "<html><head><title>Other</title></head><body><p>You followed the link.</p></body></html>"


@pytest.fixture(scope="module")
def site():
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "index.html").write_text(HTML); (d / "other.html").write_text(OTHER)
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(d), **k)
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_condense():
    assert _condense("a   b \n\n\n\n c  \n") == "a b\n\nc"


def test_open_read_links_click_fill(site):
    try:
        b = Browser(timeout_ms=10_000, max_chars=40)
        page = b.open(f"{site}/index.html")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"headless browser unavailable: {e}")
    assert page["title"] == "Test Page" and "Hello" in page["text"] and page["next_offset"] == 40
    rest = b.more(40, max_chars=10_000)
    assert "Made by JS" in page["text"] + rest["text"]              # JavaScript ran
    assert any(l["text"] == "Other page" for l in page["links"]) and page["form_fields"][0]["label"] == "Search box"
    assert b.links("other")[0]["text"] == "Other page"
    followed = b.click("other page")
    assert followed["title"] == "Other" and "followed the link" in followed["text"]
    b.open(f"{site}/index.html")
    filled = b.fill("Search box", "carpet")
    assert filled["title"] == "Test Page"
    b.close()


class _FakePage:
    """Stands in for a Playwright page; records which thread touched it (Playwright's objects are thread-bound)."""
    def __init__(self):
        self.threads = set()
    def evaluate(self, js):
        self.threads.add(threading.current_thread().name)
        return {"title": "t", "text": "body", "links": [], "inputs": [], "url": "http://x"}
    def goto(self, *a, **k):
        self.threads.add(threading.current_thread().name)
    def wait_for_load_state(self, *a, **k):
        pass
    def is_closed(self):
        return False


def _fake_browser(monkeypatch):
    b = Browser()
    pages = {}
    def ensure(c):
        if c.page is None:
            c.page = _FakePage(); c.browser = type("B", (), {"is_connected": lambda self: True, "close": lambda self: None})()
            pages[c.key] = c.page
        return c.page
    monkeypatch.setattr(Browser, "_ensure", lambda self, c: ensure(c))
    return b, pages


def test_each_context_drives_its_own_browser_from_one_thread(monkeypatch):
    """Tool calls arrive on arbitrary threads (a pool thread under the SDK engine, the caller's under the API
    engine); every Playwright call for a context must still happen on that context's own thread."""
    b, pages = _fake_browser(monkeypatch)

    def call(ctx, n):
        t = threading.current_thread()
        if ctx:
            t.ea_context = ctx
        for _ in range(n):
            b.open("http://x")
            b.more(0)
    threads = [threading.Thread(target=call, args=(ctx, 3), name=f"caller-{i}") for i, ctx in enumerate([None, None, "worker:1", "worker:1", "worker:2"])]
    for t in threads: t.start()
    for t in threads: t.join(5)
    assert set(pages) == {"main", "worker:1", "worker:2"}
    for key, page in pages.items():
        assert page.threads == {f"browser:{key}"}, (key, page.threads)   # never a caller thread
    assert b.contexts() == {"main": True, "worker:1": True, "worker:2": True}
    b.close_context("worker:1")
    assert "worker:1" not in b.contexts()
    b.close()
    assert b.contexts() == {}


def test_errors_cross_back_to_the_caller(monkeypatch):
    b, pages = _fake_browser(monkeypatch)
    monkeypatch.setattr(_FakePage, "evaluate", lambda self, js: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        b.open("http://x")
    assert b.more(0) == {"error": "no page open; use web_open first"}
    b.close()


def test_dead_chromium_is_relaunched():
    from ea.connectors.browser import _Ctx
    c = _Ctx("main")
    launches = []
    b = Browser()
    orig = Browser._ensure

    class DeadBrowser:
        def is_connected(self): return False
        def close(self): raise RuntimeError("already gone")
    c.browser, c.page = DeadBrowser(), _FakePage()
    assert not c.alive()
    import ea.connectors.browser as mod
    class FakePW:
        def start(self): launches.append(1); return self
        class chromium:
            @staticmethod
            def launch(headless):
                class Br:
                    def is_connected(self): return True
                    def new_context(self, **k):
                        class Cx:
                            def set_default_timeout(self, t): pass
                            def new_page(self): return _FakePage()
                        return Cx()
                return Br()
        def stop(self): pass
    import sys, types
    fake = types.ModuleType("playwright.sync_api"); fake.sync_playwright = lambda: FakePW()
    sys.modules["playwright.sync_api"] = fake
    try:
        page = c.run(lambda: orig(b, c))
    finally:
        del sys.modules["playwright.sync_api"]
    assert launches == [1] and c.alive() and page is c.page
    c.stop()
