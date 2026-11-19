"""Sleep Bandit (local server on :8777): nights, HRV/RHR/sleep metrics from the Oura ring, tonight's
intervention plan, per-intervention verdicts; and a read-only passthrough to any Oura v2 collection that
uses Sleep Bandit's own OAuth token (the single owner of the rotating refresh token)."""
from __future__ import annotations

from typing import Any

from .http import ServiceRouter, get_json

OURA_COLLECTIONS = ("sleep", "daily_sleep", "daily_readiness", "daily_activity", "daily_stress", "daily_spo2",
                    "heartrate", "workout", "session", "daily_resilience", "vo2_max")


class SleepBandit:
    def __init__(self, router: ServiceRouter):
        self.router = router

    def summary(self, days: int = 14, intervention: str | None = None) -> dict[str, Any]:
        """Nights and verdicts, cut down to what fits in a tool result. Sleep Bandit sends every intervention's
        per-dose table for five outcomes (100,000 characters whatever `days` is), which the CLI refused to hand to
        the model (2026-11-19). The tables come back only for the one intervention named."""
        with self.router.client("bandit") as c:
            d = get_json(c, "/api/agent/summary", days=max(1, min(days, 400)))
        d["nights"] = [_night(n) for n in d.get("nights", [])]
        items = d.get("interventions", [])
        if intervention:
            want = intervention.strip().lower()
            found = [i for i in items if str(i.get("name", "")).lower() == want] \
                or [i for i in items if want in str(i.get("name", "")).lower()]
            if not found:
                raise ValueError(f"no intervention named {intervention!r}; there are: "
                                 + ", ".join(str(i.get("name")) for i in items))
            d["interventions"] = [_rounded(i) for i in found]
        else:
            d["interventions"] = [_verdict(i) for i in items]
        return d

    def oura(self, collection: str, days: int = 7) -> dict[str, Any]:
        if collection not in OURA_COLLECTIONS:
            raise ValueError(f"collection must be one of {OURA_COLLECTIONS}")
        with self.router.client("bandit") as c:
            data = get_json(c, "/api/oura/collection", name=collection, days=max(1, min(days, 400)))
        if collection == "heartrate":          # thousands of samples: condense to hourly min/mean
            data["data"] = _hourly_hr(data.get("data", []))
        return data


OUTCOMES = {"readout": "hrv", "readout_lrhr": "lowest_resting_hr", "readout_tst": "total_sleep_hours",
            "readout_lat": "latency_min", "readout_eff": "efficiency"}


def _rounded(v: Any) -> Any:
    """Three decimals is more than the estimates are good for, and halves the text."""
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, dict):
        return {k: _rounded(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_rounded(x) for x in v]
    return v


def _night(n: dict[str, Any]) -> dict[str, Any]:
    """A night with its interventions as two short lists: the ones that were on (dose, done, what was actually
    taken when that differs) and the names of the ones that were off. An off one that was done anyway stays whole."""
    on, off = [], []
    for c in n.get("cells") or []:
        if not c.get("on") and not c.get("done") and c.get("taken_dose") is None and not c.get("excluded"):
            off.append(c.get("intervention"))
            continue
        cell = {"intervention": c.get("intervention"), "dose": c.get("assigned_dose"), "unit": c.get("unit"),
                "on": c.get("on"), "done": c.get("done")}
        if c.get("taken_dose") is not None and c.get("taken_dose") != c.get("assigned_dose"):
            cell["taken_dose"] = c["taken_dose"]
        cell.update({k: c[k] for k in ("excluded", "input_value") if c.get(k)})
        on.append({k: v for k, v in cell.items() if v not in ("", None)})
    out = {k: v for k, v in n.items() if k != "cells"}
    return _rounded({**out, "on": on, "off": off})


def _verdict(i: dict[str, Any]) -> dict[str, Any]:
    """One intervention without its per-dose tables: the full HRV verdict (the outcome the bandit optimises) and,
    for the other four outcomes, the chance it helps and the size of the effect."""
    out = {k: i.get(k) for k in ("name", "category", "kind", "unit", "active", "p_scheduled") if i.get(k) not in ("", None)}
    for key, name in OUTCOMES.items():
        r = i.get(key)
        if not isinstance(r, dict):
            continue
        keep = ("p_helps", "p_meaningful", "on_effect", "on_ci_low", "on_ci_high", "recommended_dose", "recommended_p",
                "n_on", "n_off") if key == "readout" else ("p_helps", "on_effect")
        out[name] = {k: r.get(k) for k in keep if r.get(k) is not None}
    if i.get("observational") is not None:
        out["observational"] = i["observational"]
    return _rounded(out)


def _hourly_hr(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[int]] = {}
    for s in samples:
        ts = str(s.get("timestamp", ""))[:13]   # YYYY-MM-DDTHH
        if ts and s.get("bpm"):
            buckets.setdefault(ts, []).append(int(s["bpm"]))
    return [{"hour": h, "min_bpm": min(v), "mean_bpm": round(sum(v) / len(v)), "n": len(v)} for h, v in sorted(buckets.items())]
