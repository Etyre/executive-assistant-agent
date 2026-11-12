"""Calendar, read-only, over any number of iCal feeds (Google Calendar's "Secret address in iCal format"
needs no OAuth). `CALENDAR_ICS_URLS` holds them, comma- or newline-separated; the same URLs the Project
Tracker has in its `[calendar] ics_urls`. Without them, the agent falls back to the tracker's own
single-day `/api/calendar`."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo
import logging
import time
from typing import Any

import httpx
import icalendar
import recurring_ical_events

from .. import config
from .http import SourceError
from .project_tracker import ProjectTracker


log = logging.getLogger(__name__)


class Calendar:
    def __init__(self, settings: config.Settings | None = None, tracker: ProjectTracker | None = None):
        s = settings or config.load()
        self.timezone = s.timezone
        raw = config.secret(s.sources.calendar_ics_urls_env) or ""
        self.urls = [u.strip() for u in raw.replace("\n", ",").split(",") if u.strip()]
        self.tracker = tracker
        self._cache: dict[str, tuple[float, bytes]] = {}

    def enabled(self) -> bool:
        return bool(self.urls) or self.tracker is not None

    def _fetch(self, url: str, max_age: float = 600) -> bytes:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        try:
            r = httpx.get(url, headers={"User-Agent": "executive-assistant"}, follow_redirects=True, timeout=30)
            r.raise_for_status()
        except httpx.HTTPError as e:
            if hit:
                return hit[1]
            raise SourceError(f"calendar fetch failed: {e}") from e
        self._cache[url] = (time.time(), r.content)
        return r.content

    def events(self, start: dt.date | None = None, days: int = 1) -> list[dict[str, Any]]:
        """Events from `start` for `days` days, local time, oldest first:
        [{date, title, start: 'HH:MM'|None, end, all_day, location, description}]."""
        start = start or dt.date.today()
        days = max(1, min(days, 62))
        end = start + dt.timedelta(days=days)
        if not self.urls:
            if not self.tracker:
                raise SourceError("CALENDAR_ICS_URLS is not set and no tracker is available")
            out = []
            for i in range(days):
                d = start + dt.timedelta(days=i)
                for e in self.tracker.calendar(d.isoformat()):
                    out.append({"date": d.isoformat(), "title": e.get("title"), "all_day": e.get("all_day"),
                                "start": (e.get("start") or "")[11:16] or None, "end": (e.get("end") or "")[11:16] or None})
            return out
        # The configured timezone, not the Mac's: the scheduler and the agent's "today" use it, and a laptop that
        # travels (EST on 2026-11-11) would otherwise shift every event by hours relative to them.
        local_tz = ZoneInfo(self.timezone)
        out = []
        self.last_errors: list[str] = []
        for i, url in enumerate(self.urls):
            try:
                cal = icalendar.Calendar.from_ical(self._fetch(url))
            except (SourceError, ValueError) as e:                     # one bad feed must not hide the others
                self.last_errors.append(f"calendar {i + 1}: {str(e)[:160]}")
                log.warning("calendar feed %d failed: %s", i + 1, e)
                continue
            # which calendar: the feed's own name (Google sets X-WR-CALNAME), else "calendar 1", "calendar 2", ...
            calname = str(cal.get("X-WR-CALNAME", "") or "").strip() or f"calendar {i + 1}"
            for ev in recurring_ical_events.of(cal).between(start, end):
                if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                s_ = ev.get("DTSTART").dt
                e_ = ev.get("DTEND").dt if ev.get("DTEND") else s_
                all_day = not isinstance(s_, dt.datetime)
                if all_day:
                    out.append({"date": s_.isoformat(), "title": str(ev.get("SUMMARY", "")).strip() or "(busy)", "all_day": True,
                                "calendar": calname, "start": None, "end": None, "location": str(ev.get("LOCATION", "") or ""),
                                "description": str(ev.get("DESCRIPTION", "") or "")[:300]})
                    continue
                if s_.tzinfo is None:
                    s_ = s_.replace(tzinfo=local_tz)
                if isinstance(e_, dt.datetime) and e_.tzinfo is None:
                    e_ = e_.replace(tzinfo=local_tz)
                s_l, e_l = s_.astimezone(local_tz), e_.astimezone(local_tz)
                out.append({"date": s_l.date().isoformat(), "title": str(ev.get("SUMMARY", "")).strip() or "(busy)", "all_day": False,
                            "calendar": calname, "start": s_l.strftime("%H:%M"), "end": e_l.strftime("%H:%M"),
                            "busy": str(ev.get("TRANSP", "OPAQUE")).upper() != "TRANSPARENT",
                            "location": str(ev.get("LOCATION", "") or ""), "description": str(ev.get("DESCRIPTION", "") or "")[:300]})
        out.sort(key=lambda e: (e["date"], e["start"] or ""))
        return out
