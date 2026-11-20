"""Ordering Eli a meal (ea/food.py). The browser, his sign-in and the rules for what may be bought are on the Mac,
behind the bridge; when the agent runs on the Mac itself the same code is called directly."""
from __future__ import annotations

from typing import Any

from ..config import Settings
from ..connectors.http import ServiceRouter
from .registry import ToolRegistry

STEP_TIMEOUT = 90.0     # a page load plus the wait for the site to go quiet; placing an order waits longer still


def register(reg: ToolRegistry, router: ServiceRouter, settings: Settings) -> None:
    site = settings.food.site
    local: dict[str, Any] = {}

    def step(name: str, **args: Any) -> Any:
        try:
            if router.bridge:
                return router.mac("POST", f"/mac/food/{name}", {k: v for k, v in args.items() if v is not None}, timeout=STEP_TIMEOUT)
            if "desk" not in local:
                from ..food import FoodDesk
                local["desk"] = FoodDesk(settings.food, settings.data_dir)
            return local["desk"].op(name, args)
        except Exception as e:  # noqa: BLE001 - the model gets the reason (the Mac asleep, most often)
            return {"error": f"{type(e).__name__}: {str(e)[:400]}"}

    page = ("Returns the page as it now is: `text` (paged: next_offset), and `elements`, a numbered list of what can be "
            "clicked or typed into. The numbers hold only until the page changes, so use the ones from the latest result.")

    @reg.tool("food_view", f"Ordering food for Eli on {site}, in a browser on his Mac that is signed in to his account. "
              "This looks at the current page (the site's home page to begin with). `find` keeps only the elements whose "
              "label contains that text, which is how to find a dish on a long menu. " + page + " If the result has "
              "`blocked` (a bot check) or the site asks to sign in, stop and tell Eli; never try to get past either. "
              "What Eli wants ordered, when, and what he has liked or not are in your memory, not here.",
              {"properties": {"find": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}}})
    def food_view(find: str = "", offset: int = 0) -> Any:
        return step("view", find=find, offset=offset)

    @reg.tool("food_open", f"Go to a URL on {site} (no other site can be opened in this browser). " + page,
              {"properties": {"url": {"type": "string"}}, "required": ["url"]}, writes=True)
    def food_open(url: str) -> Any:
        return step("open", url=url)

    @reg.tool("food_click", "Click element `n` of the current food-ordering page: a restaurant, a dish, an option, "
              "'Add to bag', the tip, 'Checkout'. It will not press the button that places the order (food_place_order "
              "does that). " + page, {"properties": {"n": {"type": "integer"}}, "required": ["n"]}, writes=True)
    def food_click(n: int) -> Any:
        return step("click", n=n)

    @reg.tool("food_type", "Type into field `n` of the current food-ordering page (a search box, special instructions, "
              "a custom tip), or pick an option of a drop-down by its text; `enter` presses Enter afterwards. Password "
              "and payment fields are refused: those are Eli's to fill in. " + page,
              {"properties": {"n": {"type": "integer"}, "text": {"type": "string"}, "enter": {"type": "boolean"}},
               "required": ["n", "text"]}, writes=True)
    def food_type(n: int, text: str, enter: bool = False) -> Any:
        return step("type", n=n, text=text, enter=enter)

    @reg.tool("food_scroll", "Scroll the food-ordering page down by `pages` screens (negative for up) so a long list loads "
              "more, or go back one page with `back`. " + page,
              {"properties": {"pages": {"type": "number"}, "back": {"type": "boolean"}}}, writes=True)
    def food_scroll(pages: float = 1.0, back: bool = False) -> Any:
        return step("back") if back else step("scroll", pages=pages)

    @reg.tool("food_place_order", "Place the order that is on the checkout page now: this spends Eli's money. Before "
              "calling it, read the checkout page (food_view) and check the items, the delivery address, the tip and "
              "the total; pass that total as `expected_total`. The Mac refuses when the total differs from the page's, "
              "is over the limit Eli set, or an order was already placed today, and says which. `placed: true` means "
              "the site moved on from checkout: read `page_text` for the confirmation and the arrival time, and tell "
              "Eli what is coming and when. `placed: null` means it is not known whether the order went through: do "
              "not try again; check the site's order history and tell Eli. `placed: false` with status 'failed' means "
              "the site kept the checkout page open: read it for the reason.",
              {"properties": {"expected_total": {"type": "number", "description": "The order total shown at checkout, in dollars"}},
               "required": ["expected_total"]}, writes=True)
    def food_place_order(expected_total: float) -> Any:
        return step("place", expected_total=expected_total)

    @reg.tool("food_orders", "The food orders this tool has placed or tried in the last `days` days (when, total, "
              "status), and the limits in force: the most an order may cost, orders allowed a day, orders so far today.",
              {"properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90}}})
    def food_orders(days: int = 14) -> Any:
        return step("orders", days=days)
