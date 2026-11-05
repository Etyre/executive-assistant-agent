"""Exercise sessions from the exercise-tracker app's Google Sheet (Apps Script `?action=export`).
EXERCISE_EXPORT_URL is the script's /exec URL (the same one the Project Tracker's Settings page holds).
Read-only; the sheet is written only by the phone app."""
from __future__ import annotations

import datetime as dt
import time
from typing import Any

import httpx

from .. import config
from .http import SourceError


class Exercise:
    def __init__(self, settings: config.Settings | None = None):
        s = settings or config.load()
        self.url = config.secret(s.sources.exercise_export_url_env)
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    def enabled(self) -> bool:
        return bool(self.url)

    def _fetch(self) -> list[dict[str, Any]]:
        if not self.url:
            raise SourceError("EXERCISE_EXPORT_URL is not set")
        if self._cache and time.time() - self._cache[0] < 600:
            return self._cache[1]
        try:
            r = httpx.get(self.url, params={"action": "export"}, follow_redirects=True, timeout=30)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            raise SourceError(f"exercise export: {e}") from e
        except ValueError as e:
            raise SourceError("exercise export did not return JSON (is the /exec URL right?)") from e
        if not data.get("ok"):
            raise SourceError(f"exercise export error: {data.get('error')}")
        sessions = data.get("sessions", [])
        self._cache = (time.time(), sessions)
        return sessions

    def sessions(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = dt.datetime.now().astimezone() - dt.timedelta(days=days)
        out = []
        for s in self._fetch():
            try:
                started = dt.datetime.fromisoformat(str(s.get("startedAt")).replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.astimezone()
            except (TypeError, ValueError):
                continue
            if started < cutoff:
                continue
            sets = s.get("sets") or []
            fields = {f.get("label") or f.get("key"): f.get("value") for f in (s.get("fields") or [])}
            out.append({"date": started.astimezone().date().isoformat(), "started_at": started.astimezone().isoformat(timespec="minutes"),
                        "exercise": s.get("exerciseName"), "category": s.get("category"),
                        "minutes": round((s.get("durationSec") or 0) / 60),
                        "sets": [{"reps": x.get("reps"), "weight": x.get("weight"), "unit": x.get("unit"), "warmup": x.get("warmup")} for x in sets],
                        "fields": fields, "notes": s.get("notes") or ""})
        out.sort(key=lambda x: x["started_at"], reverse=True)
        return out

    def days_since(self) -> dict[str, int | None]:
        """Days since each exercise was last done (for the cadence rules in Exercise plan.md)."""
        last: dict[str, str] = {}
        for s in self.sessions(365):
            last.setdefault(s["exercise"], s["date"])
        today = dt.date.today()
        return {k: (today - dt.date.fromisoformat(v)).days for k, v in last.items()}
