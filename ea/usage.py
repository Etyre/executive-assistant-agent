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


COMPONENTS = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output")
COMPONENT_LABELS = {"input": "uncached input", "cache_read": "cache reads", "cache_write_5m": "cache writes (5 min)",
                    "cache_write_1h": "cache writes (1 hour)", "output": "output"}


def component_costs(model: str, speed: str, t: dict[str, Any]) -> dict[str, float] | None:
    """`cost()` split into its five parts (same prices), or None for a model with no listed price."""
    price = next((p for name, p in PRICES.items() if (model or "").startswith(name)), None)
    if price is None:
        return None
    p_in, p_out, p_read = price
    per = {"input": p_in, "cache_read": p_read, "cache_write_5m": p_in * CACHE_WRITE_5M, "cache_write_1h": p_in * CACHE_WRITE_1H, "output": p_out}
    k = FAST_MULTIPLIER if speed == "fast" else 1.0
    return {c: (t.get(c) or 0) * per[c] * k / 1e6 for c in COMPONENTS}


def breakdown_report(convo: Any, days: int = 7, tz: dt.tzinfo | None = None, now: dt.datetime | None = None) -> str:
    """Where the money goes over the last `days` days: by cost component per speed (how much is cache reads, cache
    writes, output), and by kind of turn (chat, voice, proactive, worker, keepalive)."""
    now = now or dt.datetime.now(tz)
    start = (now - dt.timedelta(days=max(days, 1) - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = convo.usage_rows(start.timestamp())
    if not rows:
        return f"Cost breakdown, last {days} days: no model calls recorded."
    comp: dict[str, dict[str, list[float]]] = {}          # speed -> component -> [tokens, usd]
    modes: dict[tuple[str, str], dict[str, float]] = {}   # (mode, speed) -> totals
    for r in rows:
        cc = component_costs(r["model"], r["speed"], r) or {}
        s = comp.setdefault(r["speed"], {c: [0, 0.0] for c in COMPONENTS})
        for c in COMPONENTS:
            s[c][0] += r[c] or 0
            s[c][1] += cc.get(c, 0.0)
        m = modes.setdefault((r["mode"] or "?", r["speed"]), {"calls": 0, "tokens": 0, "usd": 0.0, "charged": 0.0})
        m["calls"] += 1
        m["tokens"] += sum(r[c] or 0 for c in COMPONENTS)
        m["usd"] += r["cost_usd"] or 0.0
        m["charged"] += (r["cost_usd"] or 0.0) if is_charged(r) else 0.0
    lines = [f"Cost by component, last {days} days (estimate at list prices; fast mode is twice the standard price on every component):",
             f"  {'speed':10}{'component':24}{'tokens':>14}{'cost':>10}{'share of that speed':>21}"]
    for speed in sorted(comp):
        total = sum(v[1] for v in comp[speed].values()) or 1.0
        for c in COMPONENTS:
            tok, usd = comp[speed][c]
            if tok:
                lines.append(f"  {speed:10}{COMPONENT_LABELS[c]:24}{tok:>14,}{'$' + format(usd, ',.2f'):>10}{usd / total:>21.0%}")
    lines += ["", f"Cost by kind of turn, last {days} days:",
              f"  {'kind':12}{'speed':10}{'calls':>7}{'tokens':>14}{'cost (list)':>13}{'charged':>10}"]
    for (mode, speed), m in sorted(modes.items()):
        lines.append(f"  {mode:12}{speed:10}{m['calls']:>7}{m['tokens']:>14,}{'$' + format(m['usd'], ',.2f'):>13}{'$' + format(m['charged'], ',.2f'):>10}")
    return "\n".join(lines)


def is_charged(row: dict[str, Any]) -> bool:
    """Is this call paid for beyond the subscription? Over the API key everything is; on the subscription only
    fast-mode calls are (usage credits), standard ones are covered by the plan."""
    return row["billing"] == "api" or row["speed"] == "fast"


COLD_START_TOKENS = 20_000        # a fast call that read nothing from the cache and wrote at least this much rewrote the conversation


def daily(convo: Any, days: int = 7, tz: dt.tzinfo | None = None, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Per calendar day (in `tz`), newest last: what was charged and why. `charged`: dollars beyond the plan;
    `chat` / `keepalive` / `other`: its split by kind of turn; `cold_starts` (+ `cold_start_usd`): fast calls that
    rewrote the conversation to the cache instead of reading it; `plan_list_usd`: plan-covered work at list price."""
    now = now or dt.datetime.now(tz)
    start = (now - dt.timedelta(days=max(days, 1) - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    out: dict[str, dict[str, Any]] = {}
    for i in range(max(days, 1)):
        d = (start + dt.timedelta(days=i)).date().isoformat()
        out[d] = {"day": d, "charged": 0.0, "chat": 0.0, "keepalive": 0.0, "other": 0.0, "fast_calls": 0, "cold_starts": 0,
                  "cold_start_usd": 0.0, "plan_list_usd": 0.0, "calls": 0}
    for r in convo.usage_rows(start.timestamp()):
        d = out.get(dt.datetime.fromtimestamp(r["ts"], tz).date().isoformat())
        if d is None:
            continue
        usd = r["cost_usd"] or 0.0
        d["calls"] += 1
        if not is_charged(r):
            d["plan_list_usd"] += usd
            continue
        d["charged"] += usd
        d["chat" if r["mode"] in ("chat", "voice") else "keepalive" if r["mode"] == "keepalive" else "other"] += usd
        if r["speed"] == "fast":
            d["fast_calls"] += 1
            if not r["cache_read"] and (r["cache_write_1h"] or 0) + (r["cache_write_5m"] or 0) >= COLD_START_TOKENS:
                d["cold_starts"] += 1
                d["cold_start_usd"] += usd
    return list(out.values())


def charged_today(convo: Any, tz: dt.tzinfo | None = None, now: dt.datetime | None = None) -> float:
    return daily(convo, 1, tz, now)[-1]["charged"]


def daily_report(convo: Any, days: int = 7, tz: dt.tzinfo | None = None, now: dt.datetime | None = None) -> str:
    rows = daily(convo, days, tz, now)
    lines = [f"Charged beyond the plan, per day (last {len(rows)} days; estimate at list prices):",
             f"  {'day':12}{'charged':>10}{'chat':>10}{'keep-alive':>12}{'other':>9}{'fast calls':>12}{'cold starts':>13}{'cold $':>9}{'plan work (list)':>18}"]
    for d in rows:
        lines.append(f"  {d['day']:12}{d['charged']:>10.2f}{d['chat']:>10.2f}{d['keepalive']:>12.2f}{d['other']:>9.2f}{d['fast_calls']:>12}"
                     f"{d['cold_starts']:>13}{d['cold_start_usd']:>9.2f}{d['plan_list_usd']:>18.2f}")
    total = sum(d["charged"] for d in rows)
    lines.append(f"  total charged ${total:,.2f}, average ${total / len(rows):,.2f} a day")
    return "\n".join(lines)


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
    """The text `python -m ea usage` prints: fast vs standard, per period. Pass `now` in the app's timezone so
    "today" is the same day as in the per-day table and the cost alert (the machine's clock may be elsewhere)."""
    now = now or dt.datetime.now()
    first = convo.usage_first_ts()
    if not first:
        return "Model usage by speed: nothing recorded yet (tracking starts with the first model call after 2026-11-14)."
    lines = [f"Model usage by speed (tracked since {dt.datetime.fromtimestamp(first, now.tzinfo):%Y-%m-%d %H:%M}; cost = estimate at list prices)"]
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
