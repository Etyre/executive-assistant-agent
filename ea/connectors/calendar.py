"""Calendar. Reads go over any number of iCal feeds (Google Calendar's "Secret address in iCal format" needs no
OAuth). `CALENDAR_ICS_URLS` holds them, comma- or newline-separated; the same URLs the Project Tracker has in its
`[calendar] ics_urls`. Without them, the agent falls back to the tracker's own single-day `/api/calendar`.

Changes (create, update, delete) go to a Google Apps Script web app that runs as the user
(`deploy/calendar_script/`): `CALENDAR_WRITE_URL` is its /exec address and `CALENDAR_WRITE_TOKEN` the shared
secret. Without both, the calendar is read-only. The script, not this file, holds the hard rules (no guests are
ever added; an event with other guests is not changed), and it is authorised only for events on calendars the
user owns (Google's calendar.events.owned scope). That scope cannot list calendars, so a calendar is named to the
script by its id, which this file reads out of the iCal feed's own address."""
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
        self.write_url = config.secret(s.sources.calendar_write_url_env) or ""
        self.write_token = config.secret(s.sources.calendar_write_token_env) or ""

    def can_write(self) -> bool:
        return bool(self.write_url and self.write_token)

    def local_iso(self, when: str) -> str:
        """'2026-11-20T15:00' in the app's timezone -> the same moment with its offset; one that has an offset is kept."""
        t = dt.datetime.fromisoformat(when)
        if t.tzinfo is None:
            t = t.replace(tzinfo=ZoneInfo(self.timezone))
        return t.isoformat()

    def calendars(self) -> list[dict[str, str]]:
        """The calendars behind the feeds: [{name, id}]. A Google feed address is .../ical/<calendar id>/private-.../basic.ics."""
        import re
        from urllib.parse import unquote
        out = []
        for i, url in enumerate(self.urls):
            m = re.search(r"/ical/([^/]+)/", url)
            try:
                name = str(icalendar.Calendar.from_ical(self._fetch(url)).get("X-WR-CALNAME", "") or "").strip()
            except (SourceError, ValueError):
                name = ""
            out.append({"name": name or f"calendar {i + 1}", "id": unquote(m.group(1)) if m else ""})
        return out

    def _calendar_id(self, calendar: str | None) -> str | None:
        """None = the user's main calendar. A name from the feeds becomes that calendar's id; an id passes through."""
        if not calendar:
            return None
        known = self.calendars()
        for c in known:
            if calendar.strip().lower() in (c["name"].lower(), c["id"].lower()) and c["id"]:
                return c["id"]
        if "@" in calendar:
            return calendar.strip()
        raise SourceError(f"no calendar named '{calendar}'; the ones in the feeds: {', '.join(c['name'] for c in known) or 'none'}")

    def write(self, action: str, calendar: str | None = None, date: str | None = None, **fields: Any) -> dict[str, Any]:
        """One change through the Apps Script. `calendar` is a feed's calendar name (or an id); `date`, for update
        and delete, is the day the event is on now. Raises SourceError with the script's own words when it refuses."""
        if action in ("update", "delete"):
            day = dt.datetime.combine(dt.date.fromisoformat(date or ""), dt.time(), ZoneInfo(self.timezone))
            fields.update(day_start=day.isoformat(), day_end=(day + dt.timedelta(days=1)).isoformat())
        elif date:
            fields["date"] = date
        if self.can_write():
            fields["calendar_id"] = self._calendar_id(calendar)
        if not self.can_write():
            raise SourceError("calendar write access is not set up: CALENDAR_WRITE_URL and CALENDAR_WRITE_TOKEN are not both set "
                              "(deploy/calendar_script/Code.gs has the steps; the user has to deploy it)")
        body = {"token": self.write_token, "action": action, **{k: v for k, v in fields.items() if v is not None}}
        try:
            r = httpx.post(self.write_url, json=body, follow_redirects=True, timeout=60)
            r.raise_for_status()
            out = r.json()
        except (httpx.HTTPError, ValueError) as e:      # an HTML page instead of JSON: the deployment is not public or the URL is wrong
            raise SourceError(f"calendar script did not answer with JSON ({str(e)[:160]}); check the deployment (Execute as: Me, access: Anyone)") from e
        if out.get("error"):
            raise SourceError(f"calendar: {out['error']}")
        self._cache.clear()                             # the next read refetches (Google's own feed can still lag some minutes)
        return out

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
            if hit:                                     # an old copy beats nothing, but the reader is told how old it is
                self.last_errors = getattr(self, "last_errors", [])
                self.last_errors.append(f"calendar feed unreachable ({type(e).__name__}); showing the copy fetched at "
                                        f"{dt.datetime.fromtimestamp(hit[0]):%Y-%m-%d %H:%M}")
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
            # Asked for a day on each side and then kept by LOCAL time. The library reads bare dates in each event's own
            # zone, and Google writes one-off events in UTC, so `between(start, end)` returned the UTC day: an event
            # at 19:00 Pacific was missing from its own day and listed under the next one's query (found 2026-11-20).
            first, last = dt.datetime.combine(start, dt.time.min, local_tz), dt.datetime.combine(end, dt.time.min, local_tz)
            for ev in recurring_ical_events.of(cal).between(start - dt.timedelta(days=1), end + dt.timedelta(days=1)):
                if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                s_ = ev.get("DTSTART").dt
                e_ = ev.get("DTEND").dt if ev.get("DTEND") else s_
                all_day = not isinstance(s_, dt.datetime)
                if all_day:
                    last_day = e_ if isinstance(e_, dt.date) and not isinstance(e_, dt.datetime) and e_ > s_ else s_ + dt.timedelta(days=1)
                    if not (s_ < end and last_day > start):         # DTEND of an all-day event is the day after its last
                        continue
                    out.append({"date": s_.isoformat(), "title": str(ev.get("SUMMARY", "")).strip() or "(busy)", "all_day": True,
                                "calendar": calname, "event_id": str(ev.get("UID", "") or ""), "start": None, "end": None, "location": str(ev.get("LOCATION", "") or ""),
                                "description": str(ev.get("DESCRIPTION", "") or "")[:300]})
                    continue
                if s_.tzinfo is None:
                    s_ = s_.replace(tzinfo=local_tz)
                if isinstance(e_, dt.datetime) and e_.tzinfo is None:
                    e_ = e_.replace(tzinfo=local_tz)
                s_l, e_l = s_.astimezone(local_tz), e_.astimezone(local_tz)
                if not (s_l < last and (e_l > first or s_l >= first)):     # starts in the range, or began earlier and is still running in it
                    continue
                out.append({"date": s_l.date().isoformat(), "title": str(ev.get("SUMMARY", "")).strip() or "(busy)", "all_day": False,
                            "calendar": calname, "event_id": str(ev.get("UID", "") or ""), "start": s_l.strftime("%H:%M"), "end": e_l.strftime("%H:%M"),
                            "busy": str(ev.get("TRANSP", "OPAQUE")).upper() != "TRANSPARENT",
                            "location": str(ev.get("LOCATION", "") or ""), "description": str(ev.get("DESCRIPTION", "") or "")[:300]})
        out.sort(key=lambda e: (e["date"], e["start"] or ""))
        return out
