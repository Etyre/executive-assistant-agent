"""Token and cost accounting per model call, split by speed (fast mode vs standard).

Every model call on either engine ends in `record()`: one row in `state.db` (`llm_usage`) with the speed the
API says it SERVED the call at (`usage.speed`, not what was asked for: a refused fast request counts as
standard), the token counts, and a cost estimate at list prices. `python -m ea usage` and `/health` read it back.

The cost is an estimate, not a bill. On the subscription, standard-speed calls are covered by the plan and only
fast-mode calls are charged (to extra-usage credits); over the API key everything is charged."""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# $ per million tokens at standard speed: (input, output, cache read). Matched by model-id prefix. A model that
# is not listed is still counted in tokens, with no cost (shown as "?"), rather than priced by guesswork.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
}
FAST_MULTIPLIER = 2.0             # fast mode is twice the standard rate on the models above (Opus 5.5: $8 / $40)
CACHE_WRITE_5M, CACHE_WRITE_1H = 1.25, 2.0    # cache writes, as multiples of the input price


def tokens(usage: dict[str, Any]) -> dict[str, int]:
    """The five counts we keep, from an Anthropic `usage` object (API response or CLI stream event)."""
    cc = usage.get("cache_creation") or {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    if not (w1h or w5m):
        w5m = int(usage.get("cache_creation_input_tokens") or 0)
    return {"input": int(usage.get("input_tokens") or 0), "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write_5m": w5m, "cache_write_1h": w1h, "output": int(usage.get("output_tokens") or 0)}


def cost(model: str, speed: str, t: dict[str, int]) -> float | None:
    price = next((p for name, p in PRICES.items() if (model or "").startswith(name)), None)
    if price is None:
        return None
    p_in, p_out, p_read = price
    usd = (t["input"] * p_in + t["cache_write_5m"] * p_in * CACHE_WRITE_5M + t["cache_write_1h"] * p_in * CACHE_WRITE_1H
           + t["cache_read"] * p_read + t["output"] * p_out) / 1e6
    return usd * (FAST_MULTIPLIER if speed == "fast" else 1.0)


def record(convo: Any, *, engine: str, billing: str, model: str, usage: dict[str, Any] | None,
           mode: str = "", channel: str = "", thread: str = "") -> None:
    """One model call. Never raises: accounting must not break a turn."""
    try:
        if not usage:
            return
        t = tokens(usage)
        if not any(t.values()):
            return
        speed = str(usage.get("speed") or "standard")
        convo.record_usage({"ts": time.time(), "engine": engine, "billing": billing, "model": model or "", "speed": speed,
                            "mode": mode, "channel": channel, "thread": thread, **t, "cost_usd": cost(model, speed, t)})
    except Exception:  # noqa: BLE001
        log.exception("usage accounting failed")


PERIODS = (("today", 0), ("last 7 days", 7), ("last 30 days", 30), ("since tracking began", None))


def _since(days: int | None, now: dt.datetime) -> float:
    if days is None:
        return 0.0
    if days == 0:
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return (now - dt.timedelta(days=days)).timestamp()


def summary(convo: Any, days: int | None = 30, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Totals per (speed, billing) for the period; `tokens` is everything the model read and wrote."""
    out = []
    for r in convo.usage_totals(_since(days, now or dt.datetime.now())):
        r["tokens"] = r["input"] + r["cache_read"] + r["cache_write"] + r["output"]
        out.append(r)
    return out


def report(convo: Any, now: dt.datetime | None = None) -> str:
    """The text `python -m ea usage` prints: fast vs standard, per period."""
    now = now or dt.datetime.now()
    first = convo.usage_first_ts()
    if not first:
        return "Model usage by speed: nothing recorded yet (tracking starts with the first model call after 2026-11-14)."
    lines = [f"Model usage by speed (tracked since {dt.datetime.fromtimestamp(first):%Y-%m-%d %H:%M}; cost = estimate at list prices)"]
    for label, days in PERIODS:
        rows = summary(convo, days, now)
        lines.append(f"\n{label}:")
        if not rows:
            lines.append("  (no model calls)")
            continue
        all_tokens = sum(r["tokens"] for r in rows) or 1
        all_cost = sum(r["cost_usd"] or 0 for r in rows)
        lines.append(f"  {'speed':9}{'billed to':22}{'calls':>6}{'input':>11}{'cache read':>12}{'cache write':>12}{'output':>10}{'tokens':>12}{'share':>7}{'cost':>10}{'share':>7}")
        for r in rows:
            billed = {"subscription": "credits (extra usage)" if r["speed"] == "fast" else "plan (not charged)", "api": "API key"}.get(r["billing"], r["billing"])
            c = r["cost_usd"]
            lines.append(f"  {r['speed']:9}{billed:22}{r['calls']:>6}{r['input']:>11,}{r['cache_read']:>12,}{r['cache_write']:>12,}{r['output']:>10,}"
                         f"{r['tokens']:>12,}{r['tokens'] / all_tokens:>7.0%}"
                         + (f"{'$' + format(c, ',.2f'):>10}{(c / all_cost if all_cost else 0):>7.0%}" if not r["unpriced"] else f"{'?':>10}{'':>7}"))
        charged = sum(r["cost_usd"] or 0 for r in rows if r["billing"] == "api" or r["speed"] == "fast")
        lines.append(f"  total {'$' + format(all_cost, ',.2f')} at list prices, of which actually charged about {'$' + format(charged, ',.2f')}")
    return "\n".join(lines)
