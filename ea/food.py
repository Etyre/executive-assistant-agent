"""Food ordering on the Mac (built 2026-11-19): the agent orders Eli a meal on a delivery site with his account,
through the signed-in browser in ea/connectors/site_browser.py. This module is the part that decides whether an
order may be placed. It runs inside the Mac bridge, beside the browser, so the checks cannot be skipped from the
server side.

The model does the choosing: it browses the site, fills the bag and gets to the checkout page with the food_*
tools. Only `place` presses the purchase button, and only when, read from the checkout page itself:
  - ordering is switched on (`[food] enabled`) and a spending limit is set,
  - fewer than `max_orders_per_day` orders were placed today (the Mac's date),
  - the page shows one total, it is the total the model says it expects, and it is within `max_total_usd`,
  - the delivery address on the page contains `FOOD_ADDRESS_CONTAINS` (data/.env), when that is set,
  - there is exactly one purchase button.
Every attempt is written to data/food_orders.jsonl before the button is pressed, so a crash between the press and
the answer counts as an order and cannot lead to a second one."""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import re
import threading
import uuid
from typing import Any

from . import config
from .connectors.site_browser import SiteBrowser, SiteError

log = logging.getLogger(__name__)
# "Total" (or "Order total") at the start of a line, then the amount on that line or the next one with text. "Subtotal"
# and "Total savings" do not match.
TOTAL = re.compile(r"(?im)^[ \t]*(?:order[ \t]+|your[ \t]+)?total[ \t]*:?\s*\$[ \t]*([\d,]+\.\d{2})[ \t]*$")
MONEY = re.compile(r"\$\s*([\d,]+\.\d{2})")
COUNTS = ("placed", "unknown", "pressing")     # the states that use up the day's allowance


class FoodDesk:
    def __init__(self, s: config.FoodSettings, data_dir: pathlib.Path, browser: Any = None):
        self.s = s
        self.ledger = pathlib.Path(data_dir) / "food_orders.jsonl"
        self.shots = pathlib.Path(data_dir) / "food"
        self.browser = browser or SiteBrowser(pathlib.Path(s.profile), list(s.hosts), s.home)
        self._lock = threading.Lock()           # one thing at a time: there is one page

    # ------------------------------------------------------------------ the ledger
    def orders(self, days: int = 14) -> list[dict[str, Any]]:
        """The latest state of each attempt in the last `days` days, oldest first."""
        if not self.ledger.exists():
            return []
        since = (dt.date.today() - dt.timedelta(days=max(0, days - 1))).isoformat()
        by_id: dict[str, dict[str, Any]] = {}
        for line in self.ledger.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            by_id[r["id"]] = {**by_id.get(r["id"], {}), **r}
        return [r for r in by_id.values() if r.get("day", "") >= since]

    def _write(self, rec: dict[str, Any]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def status(self) -> dict[str, Any]:
        today = [r for r in self.orders(1) if r.get("status") in COUNTS]
        return {"site": self.s.site, "ordering_on": bool(self.s.enabled and self.s.max_total_usd > 0),
                "max_total_usd": self.s.max_total_usd, "max_orders_per_day": self.s.max_orders_per_day,
                "orders_today": len(today), "address_check": bool(config.secret(self.s.address_env))}

    # ------------------------------------------------------------------ browsing
    def op(self, name: str, args: dict[str, Any]) -> Any:
        """One browser step for the model. Refusals come back as {"error": ...} for it to read."""
        b = self.browser
        steps = {"view": lambda: b.view(str(args.get("find") or ""), int(args.get("offset") or 0)),
                 "open": lambda: b.open(str(args["url"])),
                 "click": lambda: b.click(int(args["n"])),
                 "type": lambda: b.type(int(args["n"]), str(args["text"]), bool(args.get("enter"))),
                 "scroll": lambda: b.scroll(float(args.get("pages") or 1)),
                 "back": lambda: b.back(),
                 "close": lambda: b.close(),
                 "place": lambda: self.place(args.get("expected_total")),
                 "orders": lambda: {"status": self.status(), "orders": self.orders(int(args.get("days") or 14))}}
        if name not in steps:
            return {"error": f"unknown step {name!r}"}
        with self._lock:
            try:
                return steps[name]()
            except SiteError as e:
                return {"error": str(e)}
            except Exception as e:  # noqa: BLE001 - a Playwright timeout and the like: the model reads it and looks again
                log.warning("food %s failed: %s: %s", name, type(e).__name__, e)
                return {"error": f"{type(e).__name__}: {str(e)[:300]}"}

    # ------------------------------------------------------------------ the purchase
    def place(self, expected_total: Any) -> dict[str, Any]:
        def no(why: str, **more: Any) -> dict[str, Any]:
            log.info("food order refused: %s", why)
            return {"placed": False, "refused": why, **more}

        if not self.s.enabled or self.s.max_total_usd <= 0:
            return no("ordering is switched off on the Mac ([food] enabled and max_total_usd in config.toml)")
        try:
            want = round(float(str(expected_total).replace("$", "").replace(",", "")), 2)
        except (TypeError, ValueError):
            return no("expected_total must be the total shown at checkout, as a number")
        today = [r for r in self.orders(1) if r.get("status") in COUNTS]
        if len(today) >= self.s.max_orders_per_day:
            return no(f"{len(today)} order(s) already placed today; the limit is {self.s.max_orders_per_day} a day", today=today)
        st = self.browser.final_state()
        if len(st["buttons"]) != 1:
            return no(f"this page has {len(st['buttons'])} purchase buttons, not one; go to the checkout page first", url=st["url"])
        button = st["buttons"][0]
        totals = {round(float(m.replace(",", "")), 2) for m in TOTAL.findall(st["text"])}
        if len(totals) != 1:
            return no("could not read one total from the checkout page" + (f" (saw {sorted(totals)})" if totals else ""),
                      page_text=st["text"][-1500:])
        total = totals.pop()
        on_button = MONEY.search(button["label"])
        if on_button and round(float(on_button.group(1).replace(",", "")), 2) != total:
            return no(f"the button says ${on_button.group(1)} and the page total says ${total:.2f}")
        if abs(total - want) > 0.005:
            return no(f"the page total is ${total:.2f}, not the ${want:.2f} you expected; read the checkout page again")
        if total > self.s.max_total_usd:
            return no(f"${total:.2f} is over the limit of ${self.s.max_total_usd:.2f} an order (fees and tip included)")
        address = (config.secret(self.s.address_env) or "").strip()
        if address and address.lower() not in st["text"].lower():
            return no("the checkout page does not show the delivery address this is limited to")

        rec = {"id": uuid.uuid4().hex[:12], "day": dt.date.today().isoformat(), "at": dt.datetime.now().isoformat(timespec="seconds"),
               "status": "pressing", "total": total, "site": self.s.site, "url": st["url"], "title": st["title"]}
        self._write(rec)
        self._shot(rec["id"], "before")
        try:
            after = self.browser.press_final(button["n"])
        except Exception as e:  # noqa: BLE001 - pressed or not is unknown: it stays counted, and Eli is told
            self._write({"id": rec["id"], "status": "unknown", "note": f"{type(e).__name__}: {str(e)[:200]}"})
            log.error("food order %s: the press raised %s: %s", rec["id"], type(e).__name__, e)
            return {"placed": None, "order_id": rec["id"], "total": total,
                    "unknown": "pressing the button raised an error, so it is not known whether the order went through. "
                               "Look at the site's order history, and tell Eli."}
        self._shot(rec["id"], "after")
        if after.get("left_site"):
            status, note = "unknown", f"the site handed over to {after['url']} (a bank check?), which Eli has to finish himself"
        elif after["buttons"] and after["url"].split("?")[0] == st["url"].split("?")[0]:
            status, note = "failed", "the checkout page is still showing its purchase button; read it for the reason"
        else:
            status, note = "placed", ""
        self._write({"id": rec["id"], "status": status, "after_url": after["url"], **({"note": note} if note else {})})
        log.info("food order %s: %s, $%.2f, %s", rec["id"], status, total, after["url"])
        return {"placed": status == "placed" if status != "unknown" else None, "order_id": rec["id"], "status": status,
                "total": total, **({"note": note} if note else {}), "url": after["url"], "page_text": after["text"][:1500]}

    def _shot(self, order_id: str, when: str) -> None:
        try:
            self.shots.mkdir(parents=True, exist_ok=True)
            self.browser.screenshot(self.shots / f"{dt.date.today().isoformat()}-{order_id}-{when}.png")
        except Exception as e:  # noqa: BLE001 - a picture for the record, never a reason to stop
            log.warning("food: no screenshot %s (%s)", when, e)
