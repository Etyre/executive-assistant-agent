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
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False, no_viewport=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(s.food.home)
        print(f"Sign in to {s.food.site} in the window that opened, then close the window.")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:  # noqa: BLE001 - the whole browser was quit
            pass
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    print("Saved. The agent's browser will use this sign-in from now on.")


if __name__ == "__main__":
    main()
