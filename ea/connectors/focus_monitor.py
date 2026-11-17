"""Focus & Distraction Monitor (local FastAPI on :8790), read-only.

Metrics per day (from stats.daily_metrics): longest_focus_min, total_focus_min, fully_absorbed_spans,
longest_absorbed_min, interruption_count, distraction_count, distracted_min, active_min, switches, ...
A day runs 04:00 to 04:00.

On a server the Focus Monitor is on Eli's Mac, behind the bridge, and a sleeping Mac cannot answer. So every
answer that comes through the bridge is also kept in `<data>/focus_cache.json` (the scheduler refreshes it every
ten minutes: `refresh`), and when the Mac is away the kept copy is returned with a note saying when it was taken.
Nothing is lost by that: the monitor records the Mac's windows, so while the Mac sleeps there is nothing newer."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
from typing import Any

from .. import config
from .http import MAC_AWAY, ServiceRouter, SourceError, get_json

METRIC_KEYS = ("day", "longest_focus_min", "longest_absorbed_min", "total_focus_min", "fully_absorbed_spans",
               "meeting_focus_min", "interruption_count", "distraction_count", "distracted_min", "interrupted_min",
               "active_min", "passive_min", "correspondence_min", "short_breaks", "switches", "uncertain", "reviewed")


class FocusMonitor:
    def __init__(self, router: ServiceRouter, cache_path: pathlib.Path | None = None):
        self.router = router
        # only the server keeps a copy; on the Mac the monitor is on localhost and always there
        self.cache_path = cache_path or (config.load().data_dir / "focus_cache.json" if router.bridge else None)
        self._refreshing = False               # while refreshing, an away Mac is an error, not a reason to read the copy

    # ------------------------------------------------------------------ the kept copy
    def _cache(self) -> dict[str, Any]:
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, ValueError, AttributeError):
            return {}

    def _keep(self, **parts: Any) -> None:
        if self.cache_path is None:
            return
        c = self._cache()
        for k, v in parts.items():
            if k == "day":
                days = c.setdefault("days", {})
                days[v["day"]] = {"at": time.time(), "data": v}
                for old in sorted(days)[:-45]:              # six weeks of day detail is plenty
                    del days[old]
            else:
                c[k] = v
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(c))
        tmp.replace(self.cache_path)

    @staticmethod
    def _note(at: float) -> str:
        when = dt.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M")
        return (f"Eli's Mac is asleep or off the network, so this is the copy taken at {when} (server time). The Focus "
                "Monitor only records while the Mac is awake, so nothing newer exists unless the Mac was used since.")

    def _away(self, e: SourceError) -> bool:
        return self.cache_path is not None and not self._refreshing and MAC_AWAY[:30] in str(e)

    # ------------------------------------------------------------------ reads
    def metrics(self, days: int = 14) -> Any:
        """A list of days, oldest first. With the Mac away: {"note": ..., "days": [...]} from the kept copy."""
        days = max(1, min(days, 120))
        try:
            with self.router.client("focus") as c:
                data = get_json(c, "/api/metrics", days=days)
        except SourceError as e:
            kept = self._cache().get("metrics")
            if not self._away(e) or not kept:
                raise
            return {"note": self._note(kept["at"]), "days": kept["days"][-days:]}
        out = [{k: d.get(k) for k in METRIC_KEYS if k in d} for d in data.get("days", [])]
        if self.cache_path is not None and len(out) >= len((self._cache().get("metrics") or {}).get("days", [])):
            self._keep(metrics={"at": time.time(), "days": out})
        return out

    def day(self, day: str | None = None) -> dict[str, Any]:
        """A day's focus spans and distractions, trimmed to what an assistant needs."""
        try:
            with self.router.client("focus") as c:
                d = get_json(c, "/api/day", day=day)
        except SourceError as e:
            c = self._cache()
            kept = (c.get("days") or {}).get(day or c.get("current") or "")
            if not self._away(e):
                raise
            if not kept:
                raise SourceError(f"{e} (no copy of {day or 'today'} was kept on the server)") from e
            return {"note": self._note(kept["at"]), **kept["data"]}
        out = self._trim(d)
        if self.cache_path is not None and out.get("day"):
            self._keep(day=out, **({"current": out["day"]} if day is None else {}))
        return out

    def refresh(self) -> int:
        """Bring the kept copy up to date (scheduler, every ten minutes). Returns how many answers were kept; 0 when
        the Mac is away. Today and yesterday every time; the fortnight before that when it is missing."""
        if self.cache_path is None:
            return 0
        self._refreshing = True
        try:
            self.metrics(120)
            today = self.day()["day"]
            n, have = 2, self._cache().get("days", {})
            base = dt.date.fromisoformat(today)
            for back in range(1, 15):
                d = (base - dt.timedelta(days=back)).isoformat()
                if back == 1 or d not in have:
                    self.day(d)
                    n += 1
            return n
        except SourceError:
            return 0
        finally:
            self._refreshing = False

    @staticmethod
    def _trim(d: dict[str, Any]) -> dict[str, Any]:
        # /api/day durations are seconds (a 852 s distraction showed as "minutes": 852, Atlas 2026-11-12)
        mins = lambda sec: round(sec / 60, 1) if isinstance(sec, (int, float)) else sec
        spans = [{"start": s.get("start"), "end": s.get("end"), "minutes": mins(s.get("duration")),
                  "focused_min": s.get("focus_min"), "ended_by": s.get("ended_by"), "title": s.get("title"),
                  "summary": s.get("summary"), "detours": s.get("detours"), "fully_absorbed": s.get("fully_absorbed"),
                  "toggl": s.get("toggl")} for s in d.get("focus_spans", [])]
        return {"day": d.get("day"), "metrics": {k: d.get("metrics", {}).get(k) for k in METRIC_KEYS},
                "focus_spans": spans,
                "distractions": [{"start": x.get("start"), "end": x.get("end"), "minutes": mins(x.get("duration")), "where": x.get("where")}
                                 for x in d.get("distractions", [])][:40],
                "toggl_entries": d.get("toggl", [])[:60],
                # /api/day reports to_review as a COUNT, not a list; len() on it failed whenever
                # there was anything to review (every day-detail call from 2026-11-07 on).
                "open_review_items": int(d.get("to_review") or 0)}
