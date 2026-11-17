"""Tools over the user's metrics, read-only: Toggl, Focus Monitor, Oura (via Sleep Bandit), Sleep Bandit, exercise
log. And the calendar: read, plus create/update/delete when the calendar script is set up."""
from __future__ import annotations

from typing import Any

from ..connectors.calendar import Calendar
from ..connectors.exercise import Exercise
from ..connectors.focus_monitor import FocusMonitor
from ..connectors.http import ServiceRouter
from ..connectors.project_tracker import ProjectTracker
from ..connectors.sleep_bandit import OURA_COLLECTIONS, SleepBandit
from ..connectors.sheet_reader import SheetReader
from ..connectors.toggl import Toggl
from .registry import ToolRegistry

DAYS = {"type": "integer", "minimum": 1, "description": "How many days back to look."}


def register(reg: ToolRegistry, router: ServiceRouter, toggl: Toggl, exercise: Exercise, calendar: Calendar | None = None) -> None:
    focus, bandit = FocusMonitor(router), SleepBandit(router)
    blocks = SheetReader()

    @reg.tool("sheets_available", "The Google Sheets the user has opened to you, read-only (for one, his own log of "
              "intentionality blocks and work blocks, filled in through a form after each block): each sheet's name "
              "to use with sheet_records, its title, and every tab's row count and column headers.")
    def sheets_available() -> Any:
        return blocks.describe() if blocks.enabled() else {"error": "no readable sheets are set up (READ_SHEETS, READ_SHEETS_TOKEN)"}

    @reg.tool("sheet_records", "Rows of one of the user's read-only Google Sheets (names from sheets_available): the last "
              "`last` records, oldest first (default 20, max 1000), each as column -> value with empty cells left out, "
              "plus total_rows. `offset` skips that many from the end to page further back; `tab` picks a tab other "
              "than the first.",
              {"properties": {"sheet": {"type": "string"}, "last": {"type": "integer", "minimum": 1, "maximum": 1000},
                              "offset": {"type": "integer", "minimum": 0}, "tab": {"type": "string"}}, "required": ["sheet"]})
    def sheet_records(sheet: str, last: int = 20, offset: int = 0, tab: str | None = None) -> Any:
        from ..connectors.http import SourceError
        try:
            return blocks.rows(sheet, last, offset, tab)
        except SourceError as e:
            return {"error": str(e)}
    calendar = calendar or Calendar(tracker=ProjectTracker(router))

    @reg.tool("toggl_current", "The Toggl time entry running right now (what the user says they are doing), or null.")
    def toggl_current() -> Any:
        return toggl.current() or {"running": False}      # None rendered as a bare "ok" (Atlas, 2026-11-12)

    @reg.tool("toggl_entries", "Toggl time entries for the last N days (max 90): description, project, tags, start/stop, minutes.",
              {"properties": {"days": DAYS}, "required": ["days"]})
    def toggl_entries(days: int) -> Any:
        return toggl.entries(days)

    @reg.tool("toggl_summary", "Minutes per day per Toggl project for the last N days, plus the biggest entries.",
              {"properties": {"days": DAYS}, "required": ["days"]})
    def toggl_summary(days: int) -> Any:
        return toggl.summary(days)

    @reg.tool("focus_metrics", "Daily focus metrics from the Focus Monitor for the last N days: longest_focus_min, "
              "total_focus_min, fully_absorbed_spans, distraction_count, distracted_min, active_min, ... A day runs 04:00-04:00.",
              {"properties": {"days": DAYS}, "required": ["days"]})
    def focus_metrics(days: int) -> Any:
        return focus.metrics(days)

    @reg.tool("focus_day", "One day's focus spans (start, end, focused minutes, what ended them, summary), distractions and "
              "Toggl entries from the Focus Monitor. Omit day for today.",
              {"properties": {"day": {"type": "string", "description": "YYYY-MM-DD"}}})
    def focus_day(day: str | None = None) -> Any:
        return focus.day(day)

    @reg.tool("sleep_summary", "Sleep Bandit: the last N nights with HRV, lowest resting HR, total sleep hours, latency, "
              "efficiency, which interventions were on/done, tags; tonight's intervention checklist; HRV readiness "
              "status ('intervals' = recovered enough for hard training, 'recover' = not); per-intervention verdicts.",
              {"properties": {"days": DAYS}, "required": ["days"]})
    def sleep_summary(days: int) -> Any:
        return bandit.summary(days)

    @reg.tool("oura_data", "Raw Oura v2 collection for the last N days, via Sleep Bandit's connection. Collections: "
              + ", ".join(OURA_COLLECTIONS) + ". daily_readiness has the readiness score and contributors; daily_activity "
              "steps/active calories; daily_sleep the sleep score; heartrate is condensed to hourly min/mean bpm.",
              {"properties": {"collection": {"type": "string", "enum": list(OURA_COLLECTIONS)}, "days": DAYS},
               "required": ["collection", "days"]})
    def oura_data(collection: str, days: int) -> Any:
        return bandit.oura(collection, days)

    @reg.tool("exercise_sessions", "Exercise sessions logged in the exercise-tracker app for the last N days: exercise, category, "
              "date, minutes, sets (reps/weight) or cardio fields, notes.",
              {"properties": {"days": DAYS}, "required": ["days"]})
    def exercise_sessions(days: int) -> Any:
        return exercise.sessions(days)

    @reg.tool("exercise_days_since", "Days since each exercise was last logged (compare with the cadence rules in the exercise plan).")
    def exercise_days_since() -> Any:
        return exercise.days_since()

    @reg.tool("calendar_events", "Calendar events from start_date for `days` days, across every configured "
              "calendar (each event says which one: `calendar`, and its `event_id`): date, title, start/end "
              "(HH:MM local), all_day, location, description. Omit start_date for today; days defaults to 1, max 62.",
              {"properties": {"start_date": {"type": "string", "description": "YYYY-MM-DD"},
                              "days": {"type": "integer", "minimum": 1, "maximum": 62}}})
    def calendar_events(start_date: str | None = None, days: int = 1) -> Any:
        import datetime as dt
        start = dt.date.fromisoformat(start_date) if start_date else None
        evs = calendar.events(start, days)
        errs = getattr(calendar, "last_errors", [])
        return {"warning": "; ".join(errs), "events": evs} if errs else evs

    WHEN = {"type": "string", "description": "Local time, YYYY-MM-DDTHH:MM (the user's timezone unless an offset is given)."}

    def guard_cal(fn):
        from ..connectors.http import SourceError
        import functools

        @functools.wraps(fn)
        def inner(**kw: Any) -> Any:
            try:
                return fn(**kw)
            except (SourceError, ValueError) as e:
                return {"error": str(e)}
        return inner

    @reg.tool("calendar_writable", "The calendars in the user's feeds, by name. You can change events only on the ones "
              "the user owns (his own calendars, not ones he merely subscribes to); the rest are refused by Google.")
    @guard_cal
    def calendar_writable() -> Any:
        return {"can_write": calendar.can_write(), "calendars": [c["name"] for c in calendar.calendars()],
                "default": "omit `calendar` for the user's main calendar"}

    @reg.tool("calendar_create_event", "Put an event on the user's Google Calendar. Timed: start and end. All day: date "
              "(and end_date, the last day, for several days). `calendar` is a calendar name as calendar_events shows it; omit it for "
              "the default calendar. No guests can be added, so nothing is ever sent to anyone. The result has the "
              "event_id; calendar_events may take some minutes to show the new event.",
              {"properties": {"title": {"type": "string"}, "start": WHEN, "end": WHEN,
                              "date": {"type": "string", "description": "YYYY-MM-DD, for an all-day event"},
                              "end_date": {"type": "string", "description": "YYYY-MM-DD, last day inclusive"},
                              "calendar": {"type": "string"}, "location": {"type": "string"}, "description": {"type": "string"}},
               "required": ["title"]}, writes=True)
    @guard_cal
    def calendar_create_event(title: str, start: str | None = None, end: str | None = None, date: str | None = None,
                              end_date: str | None = None, **more: Any) -> Any:      # `calendar` arrives in more: the name is taken here
        if not date and not (start and end):
            return {"error": "give start and end, or date for an all-day event"}
        return calendar.write("create", title=title, start=calendar.local_iso(start) if start and not date else None,
                              end=calendar.local_iso(end) if end and not date else None, date=date, end_date=end_date,
                              calendar=more.get("calendar"), location=more.get("location"), description=more.get("description"))

    @reg.tool("calendar_update_event", "Change an event on the user's calendar: title, start, end, location, description "
              "(only what you give changes). Identify it by event_id and date, both from calendar_events (date is the day "
              "it is on now), plus `calendar` when it is not on the default calendar. For a repeating event only that "
              "day's occurrence changes. An event with other guests is refused: tell the user to change it himself.",
              {"properties": {"event_id": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD, the day the event is on now"},
                              "calendar": {"type": "string"}, "title": {"type": "string"}, "start": WHEN, "end": WHEN,
                              "location": {"type": "string"}, "description": {"type": "string"}},
               "required": ["event_id", "date"]}, writes=True)
    @guard_cal
    def calendar_update_event(event_id: str, date: str, start: str | None = None, end: str | None = None, **more: Any) -> Any:
        return calendar.write("update", event_id=event_id, date=date, start=calendar.local_iso(start) if start else None,
                              end=calendar.local_iso(end) if end else None,
                              **{k: more[k] for k in ("calendar", "title", "location", "description") if more.get(k) is not None})

    @reg.tool("calendar_delete_event", "Delete an event from the user's calendar, identified like calendar_update_event "
              "(event_id, date, calendar). For a repeating event only that day's occurrence goes. An event with other "
              "guests is refused. Google keeps a deleted event in the calendar's Trash for 30 days.",
              {"properties": {"event_id": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
                              "calendar": {"type": "string"}},
               "required": ["event_id", "date"]}, writes=True)
    @guard_cal
    def calendar_delete_event(event_id: str, date: str, **more: Any) -> Any:
        return calendar.write("delete", event_id=event_id, date=date, calendar=more.get("calendar"))
