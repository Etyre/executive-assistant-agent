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


# ---------------------------------------------------------------- write access (the Apps Script in deploy/calendar_script)
def _writable(monkeypatch, answer):
    from ea.connectors.exercise import Exercise
    from ea.connectors.http import ServiceRouter
    from ea.connectors.toggl import Toggl
    from ea.tools import data_tools
    from ea.tools.registry import ToolRegistry
    monkeypatch.setenv("CALENDAR_WRITE_URL", "https://script.example/exec")
    monkeypatch.setenv("CALENDAR_WRITE_TOKEN", "tok")
    config.load.cache_clear()
    s = config.load()
    s.timezone = "America/Los_Angeles"
    sent = []

    class R:
        def raise_for_status(self): pass
        def json(self): return answer

    monkeypatch.setattr(cal_mod.httpx, "post", lambda url, json=None, **kw: sent.append((url, json)) or R())
    c = cal_mod.Calendar(s, tracker=None)
    reg = ToolRegistry()
    data_tools.register(reg, ServiceRouter(s), Toggl(s), Exercise(s), c)
    return reg, c, sent


def test_create_sends_local_times_with_the_app_timezone_offset(monkeypatch):
    reg, c, sent = _writable(monkeypatch, {"created": {"event_id": "abc@google.com"}})
    c._cache["feed"] = (0.0, b"old")
    out, err = reg.dispatch("calendar_create_event", {"title": "Dentist", "start": "2026-11-20T15:00", "end": "2026-11-20T16:00", "calendar": "Personal"})
    assert not err and "abc@google.com" in out
    assert sent == [("https://script.example/exec", {"token": "tok", "action": "create", "title": "Dentist", "calendar": "Personal",
                                                    "start": "2026-11-20T15:00:00-08:00", "end": "2026-11-20T16:00:00-08:00"})]
    assert c._cache == {}                               # the next read refetches the feed
    reg.dispatch("calendar_create_event", {"title": "Trip", "date": "2026-11-21", "end_date": "2026-11-23"})
    assert sent[-1][1] == {"token": "tok", "action": "create", "title": "Trip", "date": "2026-11-21", "end_date": "2026-11-23"}
    out, _ = reg.dispatch("calendar_create_event", {"title": "No time"})
    assert "give start and end" in out and len(sent) == 2


def test_update_and_delete_name_the_event_by_id_and_day(monkeypatch):
    reg, c, sent = _writable(monkeypatch, {"updated": {}})
    reg.dispatch("calendar_update_event", {"event_id": "abc@google.com", "date": "2026-11-20", "start": "2026-11-20T17:00", "title": "Dentist (moved)"})
    reg.dispatch("calendar_delete_event", {"event_id": "abc@google.com", "date": "2026-11-20"})
    assert [b for _, b in sent] == [
        {"token": "tok", "action": "update", "event_id": "abc@google.com", "date": "2026-11-20", "start": "2026-11-20T17:00:00-08:00", "title": "Dentist (moved)"},
        {"token": "tok", "action": "delete", "event_id": "abc@google.com", "date": "2026-11-20"}]


def test_a_refusal_from_the_script_reaches_the_agent_in_its_own_words(monkeypatch):
    reg, c, sent = _writable(monkeypatch, {"error": "this event has 2 other guest(s); change it by hand so they are not surprised"})
    out, _ = reg.dispatch("calendar_delete_event", {"event_id": "x", "date": "2026-11-20"})
    assert "2 other guest(s)" in out


def test_without_the_script_the_calendar_is_read_only_and_says_how_to_fix_it(monkeypatch):
    reg, c, sent = _writable(monkeypatch, {})
    c.write_url = ""
    out, _ = reg.dispatch("calendar_create_event", {"title": "X", "date": "2026-11-21"})
    assert "not set up" in out and sent == []
