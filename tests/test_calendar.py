import datetime as dt

import httpx
import pytest

from ea import config
from ea.connectors import calendar as cal_mod

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:1
SUMMARY:Standup
DTSTART;TZID=America/Los_Angeles:20261105T100000
DTEND;TZID=America/Los_Angeles:20261105T103000
RRULE:FREQ=WEEKLY;BYDAY=TH
LOCATION:Zoom
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Offsite
DTSTART;VALUE=DATE:20261106
DTEND;VALUE=DATE:20261107
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART;TZID=America/Los_Angeles:20261105T140000
DTEND;TZID=America/Los_Angeles:20261105T150000
END:VEVENT
END:VCALENDAR
"""


def test_ics_range_query(monkeypatch):
    monkeypatch.setenv("CALENDAR_ICS_URLS", "https://cal.example/a.ics, https://cal.example/b.ics")
    config.load.cache_clear()
    monkeypatch.setattr(cal_mod.httpx, "get", lambda url, **kw: httpx.Response(200, content=ICS.encode(), request=httpx.Request("GET", url)))
    c = cal_mod.Calendar(config.load())
    assert len(c.urls) == 2
    ev = c.events(dt.date(2026, 11, 5), days=10)
    titles = [(e["date"], e["title"], e["start"]) for e in ev]
    # weekly standup recurs on both Thursdays in range (twice, once per feed), the all-day offsite appears, the cancelled one does not
    assert ("2026-11-05", "Standup", "10:00") in titles and ("2026-11-12", "Standup", "10:00") in titles
    assert any(e["title"] == "Offsite" and e["all_day"] for e in ev)
    assert not any(e["title"] == "Cancelled thing" for e in ev)
    assert next(e for e in ev if e["title"] == "Standup")["location"] == "Zoom"
    assert ev == sorted(ev, key=lambda e: (e["date"], e["start"] or ""))
    # a second call within the cache window does not refetch
    monkeypatch.setattr(cal_mod.httpx, "get", lambda url, **kw: (_ for _ in ()).throw(AssertionError("refetched")))
    assert c.events(dt.date(2026, 11, 5), days=1)


def test_fallback_to_tracker_when_no_urls(monkeypatch):
    monkeypatch.delenv("CALENDAR_ICS_URLS", raising=False)
    config.load.cache_clear()

    class T:
        def calendar(self, date):
            return [{"title": "Dentist", "start": f"{date}T14:00:00-08:00", "end": f"{date}T15:00:00-08:00", "all_day": False}]

    c = cal_mod.Calendar(config.load(), tracker=T())
    ev = c.events(dt.date(2026, 11, 5), days=2)
    assert [(e["date"], e["start"]) for e in ev] == [("2026-11-05", "14:00"), ("2026-11-06", "14:00")]
    with pytest.raises(cal_mod.SourceError):
        cal_mod.Calendar(config.load(), tracker=None).events()
