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
