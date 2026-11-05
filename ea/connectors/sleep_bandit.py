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

    def summary(self, days: int = 14) -> dict[str, Any]:
        with self.router.client("bandit") as c:
            return get_json(c, "/api/agent/summary", days=max(1, min(days, 400)))

    def oura(self, collection: str, days: int = 7) -> dict[str, Any]:
        if collection not in OURA_COLLECTIONS:
            raise ValueError(f"collection must be one of {OURA_COLLECTIONS}")
        with self.router.client("bandit") as c:
            data = get_json(c, "/api/oura/collection", name=collection, days=max(1, min(days, 400)))
        if collection == "heartrate":          # thousands of samples: condense to hourly min/mean
            data["data"] = _hourly_hr(data.get("data", []))
        return data


def _hourly_hr(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[int]] = {}
    for s in samples:
        ts = str(s.get("timestamp", ""))[:13]   # YYYY-MM-DDTHH
        if ts and s.get("bpm"):
            buckets.setdefault(ts, []).append(int(s["bpm"]))
    return [{"hour": h, "min_bpm": min(v), "mean_bpm": round(sum(v) / len(v)), "n": len(v)} for h, v in sorted(buckets.items())]
