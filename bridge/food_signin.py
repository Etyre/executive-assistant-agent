#!/usr/bin/env python3
"""Opens the food-ordering browser as a window so Eli can sign in to the delivery site himself (the agent never
sees or types the password). Run it again whenever the site has signed the browser out.

    .venv/bin/python bridge/food_signin.py

Sign in, tick "keep me signed in", check that the delivery address and the card on the account are the ones the
agent should use, then close the window. The sign-in is kept in the browser's own Chrome profile ([food] profile
in config.toml), which the bridge then uses headless. Only one Chrome can hold a profile, so this first asks a
running bridge to let go of it."""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from ea import config  # noqa: E402


# Signed in, as far as the page shows: not on the sign-in page, and no "Sign in" button in the header.
SIGNED_IN_JS = """() => !location.pathname.startsWith('/login') && document.readyState === 'complete' &&
  !!document.querySelector('header, nav') &&
  ![...document.querySelectorAll('button, a')].some(e => /^(sign in|login|log in)$/i.test((e.innerText || '').trim()))"""


def main() -> None:
    s = config.load()
    try:                                    # a running bridge has the profile open after a recent order
        import httpx
        sys.path.insert(0, str(REPO / "bridge"))
        import mac_bridge
        host = mac_bridge._tailscale_ip()
        if host and mac_bridge.token():
            httpx.post(f"http://{host}:{mac_bridge.PORT}/mac/food/close", headers={"X-Bridge-Token": mac_bridge.token()}, timeout=20)
    except Exception as e:  # noqa: BLE001 - no bridge running is fine
        print(f"(could not ask the bridge to close its browser: {type(e).__name__}; carrying on)")
    from playwright.sync_api import sync_playwright
    profile = pathlib.Path(s.food.profile).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    seen = False
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False, no_viewport=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(s.food.home)
        print(f"Sign in to {s.food.site} in the window that opened, then close the window.", flush=True)
        while True:                         # until the window is closed: say when the site shows a signed-in page
            try:
                page.wait_for_timeout(2000)
                if not seen and page.evaluate(SIGNED_IN_JS):
                    seen = True
                    print("The window now shows a signed-in page. Close it when you are ready.", flush=True)
            except Exception:  # noqa: BLE001 - the window (or the whole browser) was closed
                break
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        # The check that matters: a fresh, hidden Chrome on the same profile, as the agent will use it.
        ctx = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(s.food.home.rstrip("/") + "/account/profile", wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        kept = "/login" not in page.url
        ctx.close()
    if not kept:
        sys.exit("NOT signed in: " + ("the window showed a signed-in page, but the sign-in did not survive closing it "
                 "(was 'Keep me signed in' ticked?)." if seen else "the window never showed a signed-in page."))
    print("Signed in, and the sign-in was still there in a fresh hidden browser. The agent will use it from now on.")


if __name__ == "__main__":
    main()
