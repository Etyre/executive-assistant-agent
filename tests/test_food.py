"""Food ordering (ea/food.py, ea/connectors/site_browser.py): the purchase button is pressed only inside the limits
Eli set, and the browser itself refuses the things the model must never do."""
import http.server
import json
import threading

import pytest

from ea import config
from ea.connectors.site_browser import FINAL, SiteBrowser, SiteError
from ea.food import FoodDesk

CHECKOUT = "Review your order\nDeliver to\n123 Example St NW\nItems subtotal\n$14.00\nDelivery fee\n$1.99\nTip\n$3.00\nTotal\n$21.47\n"


class FakeBrowser:
    def __init__(self, text=CHECKOUT, buttons=None, after=None, boom=False):
        self.text, self.pressed, self.boom = text, [], boom
        self.buttons = [{"n": 7, "kind": "button", "label": "Place your order"}] if buttons is None else buttons
        self.after = after or {"url": "https://shop.test/order/99/confirmed", "title": "Thanks", "text": "Order placed. Arriving 12:40pm", "buttons": []}

    def final_state(self):
        return {"url": "https://shop.test/checkout?x=1", "title": "Checkout", "text": self.text, "buttons": self.buttons}

    def press_final(self, n):
        self.pressed.append(n)
        if self.boom:
            raise TimeoutError("page went away")
        return self.after

    def screenshot(self, path):
        pass


def desk(tmp_path, browser=None, **over):
    s = config.FoodSettings(enabled=True, max_total_usd=30.0, **over)
    return FoodDesk(s, tmp_path, browser or FakeBrowser())


def test_places_an_order_inside_the_limits_and_records_it(tmp_path):
    d = desk(tmp_path)
    out = d.place(21.47)
    assert out["placed"] is True and out["total"] == 21.47 and "Arriving" in out["page_text"]
    assert d.browser.pressed == [7]
    (rec,) = d.orders()
    assert rec["status"] == "placed" and rec["total"] == 21.47
    assert d.status()["orders_today"] == 1


@pytest.mark.parametrize("kw, expected, why", [
    ({}, 20.00, "not the $20.00 you expected"),
    ({"max_total_usd": 20.0}, 21.47, "over the limit"),
    ({"enabled": False}, 21.47, "switched off"),
    ({"max_total_usd": 0.0}, 21.47, "switched off"),
    ({}, "lunch", "as a number"),
])
def test_refuses_and_presses_nothing(tmp_path, kw, expected, why):
    s = config.FoodSettings(**{"enabled": True, "max_total_usd": 30.0, **kw})
    d = FoodDesk(s, tmp_path, FakeBrowser())
    out = d.place(expected)
    assert out["placed"] is False and why in out["refused"]
    assert d.browser.pressed == [] and d.orders() == []


def test_one_order_a_day(tmp_path):
    d = desk(tmp_path)
    assert d.place(21.47)["placed"] is True
    again = d.place(21.47)
    assert again["placed"] is False and "already placed today" in again["refused"]
    assert d.browser.pressed == [7]


def test_needs_one_button_and_one_readable_total(tmp_path):
    assert "0 purchase buttons" in desk(tmp_path, FakeBrowser(buttons=[])).place(21.47)["refused"]
    two = [{"n": 1, "label": "Place order"}, {"n": 2, "label": "Place order"}]
    assert "2 purchase buttons" in desk(tmp_path, FakeBrowser(buttons=two)).place(21.47)["refused"]
    assert "could not read one total" in desk(tmp_path, FakeBrowser(text="Items subtotal\n$14.00\n")).place(14.0)["refused"]
    both = CHECKOUT + "Order total\n$25.00\n"
    assert "could not read one total" in desk(tmp_path, FakeBrowser(text=both)).place(21.47)["refused"]
    b = FakeBrowser(buttons=[{"n": 7, "label": "Place your order: $19.00"}])
    assert "the button says" in desk(tmp_path, b).place(21.47)["refused"] and b.pressed == []


def test_subtotal_is_not_the_total(tmp_path):
    d = desk(tmp_path, FakeBrowser(text="Subtotal\n$14.00\nTotal: $21.47\n"))
    assert d.place(21.47)["placed"] is True


def test_address_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("FOOD_ADDRESS_CONTAINS", "500 Other Ave")
    d = desk(tmp_path)
    assert "delivery address" in d.place(21.47)["refused"] and d.browser.pressed == []
    monkeypatch.setenv("FOOD_ADDRESS_CONTAINS", "123 example st")
    assert d.place(21.47)["placed"] is True


def test_a_press_that_raises_counts_as_an_order(tmp_path):
    d = desk(tmp_path, FakeBrowser(boom=True))
    out = d.place(21.47)
    assert out["placed"] is None and "not known" in out["unknown"]
    assert d.orders()[0]["status"] == "unknown"
    assert "already placed today" in d.place(21.47)["refused"]        # no second try on the same day


def test_checkout_that_stays_open_is_a_failure_and_may_be_retried(tmp_path):
    stuck = {"url": "https://shop.test/checkout", "title": "Checkout", "text": "Your card was declined", "buttons": [{"n": 7, "label": "Place your order"}]}
    d = desk(tmp_path, FakeBrowser(after=stuck))
    out = d.place(21.47)
    assert out["placed"] is False and out["status"] == "failed" and "declined" in out["page_text"]
    assert d.status()["orders_today"] == 0


def test_leaving_the_site_after_the_press_is_unknown(tmp_path):
    d = desk(tmp_path, FakeBrowser(after={"url": "https://bank.test/3ds", "title": "", "text": "", "buttons": [], "left_site": True}))
    assert d.place(21.47)["placed"] is None and d.status()["orders_today"] == 1


def test_which_labels_are_the_purchase_button():
    for yes in ("Place order", "Place your order", "Place your order: $23.10", "Submit order", "Complete my order", "Pay $21.47", "Pay now", "Buy now"):
        assert FINAL.search(yes), yes
    for no in ("Order again", "View order", "Add to bag", "Checkout", "Proceed to checkout", "Pay with a new card", "Start group order", "Preorder"):
        assert not FINAL.search(no), no


def test_which_tools_exist_in_each_mode():
    from ea.tools import food_tools
    from ea.tools.registry import ToolRegistry

    class Router:
        bridge = ""
    browse = {"food_view", "food_open", "food_click", "food_type", "food_scroll"}
    reg, s = ToolRegistry(), config.Settings()          # no spending limit: the agent chooses, Eli orders
    food_tools.register(reg, Router(), s)
    assert {t.name for t in reg.specs()} == browse
    assert "you cannot order" in next(t.description for t in reg.specs() if t.name == "food_view")
    reg, s = ToolRegistry(), config.Settings()
    s.food.max_total_usd = 30.0
    food_tools.register(reg, Router(), s)
    assert {t.name for t in reg.specs()} == browse | {"food_place_order", "food_orders"}


# ---------------------------------------------------------------- the real browser against a page of our own
PAGES = {
    "/": """<html><title>Shop</title><body><main><h1>Menu</h1>
        <button onclick="document.getElementById('bag').innerText='1 item'">Add to bag Lentil soup $9.00</button>
        <p id=bag>empty</p><a href="/checkout">Checkout</a> <a href="http://localhost:{port}/elsewhere">Partner site</a>
        <label>Special instructions <input id=note></label><label>Password <input type=password id=pw></label>
        <label>Card number <input autocomplete="cc-number" id=card></label></main></body></html>""",
    "/checkout": """<html><title>Checkout</title><body><main><p>Total</p><p>$9.00</p>
        <button onclick="location.href='/done'">Place your order</button></main></body></html>""",
    "/done": "<html><title>Done</title><body><main>Order placed</main></body></html>",
    "/elsewhere": "<html><body>somewhere else</body></html>",
}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = PAGES.get(self.path.split("?")[0], "missing").replace("{port}", str(self.server.server_port)).encode()
            self.send_response(200); self.send_header("content-type", "text/html"); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = f"http://127.0.0.1:{srv.server_port}/"
    b = SiteBrowser(tmp_path_factory.mktemp("profile"), ["127.0.0.1"], home)
    try:
        b.view()
    except Exception as e:  # noqa: BLE001 - no Chrome on this machine (the server's image has only Chromium)
        b.close(); srv.shutdown()
        pytest.skip(f"Chrome is not available to Playwright here: {type(e).__name__}")
    yield b, home
    b.close(); srv.shutdown()


def n(view, text):
    return next(e["n"] for e in view["elements"] if text.lower() in e["label"].lower())


def test_browser_reads_clicks_and_types(site):
    b, home = site
    v = b.open(home)
    assert v["title"] == "Shop" and "Menu" in v["text"]
    v = b.click(n(v, "Lentil soup"))
    assert "1 item" in v["text"]
    v = b.type(n(v, "Special instructions"), "no cilantro")
    assert next(e for e in v["elements"] if e["label"] == "Special instructions")["value"] == "no cilantro"
    assert [e["label"] for e in b.view(find="lentil")["elements"]] == ["Add to bag Lentil soup $9.00"]


def test_browser_refuses_secrets_other_sites_and_the_purchase_button(site):
    b, home = site
    v = b.open(home)
    for field in ("Password", "Card number"):
        with pytest.raises(SiteError, match="password or payment"):
            b.type(n(v, field), "x")
    with pytest.raises(SiteError, match="not allowed to visit"):
        b.click(n(v, "Partner site"))
    assert b.view()["url"].startswith(home)                       # and it came back
    with pytest.raises(SiteError, match="only visits"):
        b.open("https://example.com/")
    v = b.click(n(b.view(), "Checkout"))
    with pytest.raises(SiteError, match="completes a purchase"):
        b.click(n(v, "Place your order"))
    assert b.view()["title"] == "Checkout"


def test_the_whole_purchase_through_the_desk(site, tmp_path):
    b, home = site
    b.open(home + "checkout")
    d = FoodDesk(config.FoodSettings(enabled=True, max_total_usd=10.0), tmp_path, b)
    assert "not the $8.00" in d.op("place", {"expected_total": 8})["refused"]
    out = d.op("place", {"expected_total": 9})
    assert out["placed"] is True and "Order placed" in out["page_text"] and out["url"].endswith("/done")
    assert json.loads((tmp_path / "food_orders.jsonl").read_text().splitlines()[-1])["status"] == "placed"
    assert list((tmp_path / "food").glob("*-before.png")) and list((tmp_path / "food").glob("*-after.png"))
