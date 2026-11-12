"""Read-only tools over the user's metrics: Toggl, Focus Monitor, Oura (via Sleep Bandit), Sleep Bandit,
exercise log."""
from __future__ import annotations

from typing import Any

from ..connectors.calendar import Calendar
from ..connectors.exercise import Exercise
from ..connectors.focus_monitor import FocusMonitor
from ..connectors.http import ServiceRouter
from ..connectors.project_tracker import ProjectTracker
from ..connectors.sleep_bandit import OURA_COLLECTIONS, SleepBandit
from ..connectors.toggl import Toggl
from .registry import ToolRegistry

DAYS = {"type": "integer", "minimum": 1, "description": "How many days back to look."}


def register(reg: ToolRegistry, router: ServiceRouter, toggl: Toggl, exercise: Exercise, calendar: Calendar | None = None) -> None:
    focus, bandit = FocusMonitor(router), SleepBandit(router)
    calendar = calendar or Calendar(tracker=ProjectTracker(router))

    @reg.tool("toggl_current", "The Toggl time entry running right now (what the user says they are doing), or null.")
    def toggl_current() -> Any:
        return toggl.current()

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

    @reg.tool("calendar_events", "Calendar events (read-only) from start_date for `days` days, across every configured "
              "calendar (each event says which one: `calendar`): date, title, start/end "
              "(HH:MM local), all_day, location, description. Omit start_date for today; days defaults to 1, max 62.",
              {"properties": {"start_date": {"type": "string", "description": "YYYY-MM-DD"},
                              "days": {"type": "integer", "minimum": 1, "maximum": 62}}})
    def calendar_events(start_date: str | None = None, days: int = 1) -> Any:
        import datetime as dt
        start = dt.date.fromisoformat(start_date) if start_date else None
        evs = calendar.events(start, days)
        errs = getattr(calendar, "last_errors", [])
        return {"warning": "; ".join(errs), "events": evs} if errs else evs
