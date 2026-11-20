"""Toggl Track v9, read-only. Token: TOGGL_API_TOKEN (Toggl -> Profile -> API token)."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

import httpx

from .. import config
from .http import ServiceRouter, SourceError, get_json

API = "https://api.track.toggl.com/api/v9"


class Toggl:
    """Toggl time entries. Two sources: the Focus Monitor's local copy (default since 2026-11-11: it already syncs
    Toggl every 5 minutes, and the agent's own polling kept exhausting the shared hourly API quota), or Toggl's
    API directly (`sources.toggl_source = "toggl"`). Same shapes either way."""

    def __init__(self, settings: config.Settings | None = None, router: ServiceRouter | None = None):
        s = settings or config.load()
        self.token = config.secret(s.sources.toggl_token_env)
        self.via_focus = s.sources.toggl_source == "focus_monitor"
        self.router = router or (ServiceRouter(s) if self.via_focus else None)
        self._projects: dict[int, str] | None = None

    def enabled(self) -> bool:
        return self.via_focus or bool(self.token)

    def describe(self) -> str:
        return "via Focus Monitor (its 5-minute Toggl sync)" if self.via_focus else "Toggl API v9"

    def _client(self) -> httpx.Client:
        if not self.token:
            raise SourceError("TOGGL_API_TOKEN is not set")
        return httpx.Client(base_url=API, auth=(self.token, "api_token"), timeout=20)

    def _get(self, path: str, **params: Any) -> Any:
        with self._client() as c:
            try:
                r = c.get(path, params=params)
                r.raise_for_status()
                return r.json() if r.content else None
            except httpx.HTTPStatusError as e:
                raise SourceError(f"Toggl {path} -> {e.response.status_code}: {e.response.text[:200]}") from e
            except httpx.HTTPError as e:
                raise SourceError(f"Toggl {path}: {e}") from e

    def projects(self) -> dict[int, str]:
        if self._projects is None:
            self._projects = {p["id"]: p["name"] for p in (self._get("/me/projects") or [])}
        return self._projects

    def current(self) -> dict[str, Any] | None:
        if self.via_focus:
            with self.router.client("focus") as c:
                return get_json(c, "/api/toggl/current") or None
        e = self._get("/me/time_entries/current")
        return self._shape(e) if e else None

    def entries(self, days: int = 7) -> list[dict[str, Any]]:
        days = max(1, min(days, 90))
        if self.via_focus:
            with self.router.client("focus") as c:
                return list(get_json(c, "/api/toggl/entries", days=days) or [])
        # Two days ahead, not one: Toggl reads a bare date as midnight UTC, which is the afternoon in the Americas, so
        # with tomorrow as the end this evening's entries would be left out. Extra future days cost nothing.
        end = dt.date.today() + dt.timedelta(days=2)
        start = dt.date.today() - dt.timedelta(days=days)
        raw = self._get("/me/time_entries", start_date=start.isoformat(), end_date=end.isoformat()) or []
        return [self._shape(e) for e in raw]

    def _shape(self, e: dict[str, Any]) -> dict[str, Any]:
        start = e.get("start")
        stop = e.get("stop")
        dur = e.get("duration", 0)
        running = dur is not None and dur < 0
        if running and start:
            s = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            dur = int((dt.datetime.now(dt.timezone.utc) - s).total_seconds())
        return {"id": e.get("id"), "description": e.get("description") or "", "project": self.projects().get(e.get("project_id")),
                "tags": e.get("tags") or [], "start": start, "stop": stop, "minutes": round((dur or 0) / 60),
                "running": running}

    def summary(self, days: int = 7) -> dict[str, Any]:
        """Minutes per day per project, plus per description, for a quick look at where time went."""
        per_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        per_desc: dict[str, int] = defaultdict(int)
        total = 0
        for e in self.entries(days):
            if not e["start"]:
                continue
            day = dt.datetime.fromisoformat(e["start"].replace("Z", "+00:00")).astimezone().date().isoformat()
            key = e["project"] or "(no project)"
            per_day[day][key] += e["minutes"]
            per_desc[f"{key} / {e['description']}"] += e["minutes"]
            total += e["minutes"]
        top = sorted(per_desc.items(), key=lambda kv: -kv[1])[:20]
        return {"days": days, "total_minutes": total,
                "per_day": {d: dict(sorted(v.items(), key=lambda kv: -kv[1])) for d, v in sorted(per_day.items())},
                "top_entries": [{"entry": k, "minutes": v} for k, v in top]}
