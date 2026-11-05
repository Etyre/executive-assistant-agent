"""Focus & Distraction Monitor (local FastAPI on :8790), read-only.

Metrics per day (from stats.daily_metrics): longest_focus_min, total_focus_min, fully_absorbed_spans,
longest_absorbed_min, interruption_count, distraction_count, distracted_min, active_min, switches, ...
A day runs 04:00 to 04:00."""
from __future__ import annotations

from typing import Any

from .http import ServiceRouter, get_json

METRIC_KEYS = ("day", "longest_focus_min", "longest_absorbed_min", "total_focus_min", "fully_absorbed_spans",
               "meeting_focus_min", "interruption_count", "distraction_count", "distracted_min", "interrupted_min",
               "active_min", "passive_min", "correspondence_min", "short_breaks", "switches", "uncertain", "reviewed")


class FocusMonitor:
    def __init__(self, router: ServiceRouter):
        self.router = router

    def metrics(self, days: int = 14) -> list[dict[str, Any]]:
        with self.router.client("focus") as c:
            data = get_json(c, "/api/metrics", days=max(1, min(days, 120)))
        return [{k: d.get(k) for k in METRIC_KEYS if k in d} for d in data.get("days", [])]

    def day(self, day: str | None = None) -> dict[str, Any]:
        """A day's focus spans and distractions, trimmed to what an assistant needs."""
        with self.router.client("focus") as c:
            d = get_json(c, "/api/day", day=day)
        spans = [{"start": s.get("start"), "end": s.get("end"), "minutes": s.get("duration"),
                  "focused_min": s.get("focus_min"), "ended_by": s.get("ended_by"), "title": s.get("title"),
                  "summary": s.get("summary"), "detours": s.get("detours"), "fully_absorbed": s.get("fully_absorbed"),
                  "toggl": s.get("toggl")} for s in d.get("focus_spans", [])]
        return {"day": d.get("day"), "metrics": {k: d.get("metrics", {}).get(k) for k in METRIC_KEYS},
                "focus_spans": spans,
                "distractions": [{"start": x.get("start"), "end": x.get("end"), "minutes": x.get("duration"), "where": x.get("where")}
                                 for x in d.get("distractions", [])][:40],
                "toggl_entries": d.get("toggl", [])[:60],
                "open_review_items": len(d.get("to_review") or [])}
