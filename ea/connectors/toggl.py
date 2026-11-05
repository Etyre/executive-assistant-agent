"""Toggl Track v9, read-only. Token: TOGGL_API_TOKEN (Toggl -> Profile -> API token)."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

import httpx

from .. import config
from .http import SourceError

API = "https://api.track.toggl.com/api/v9"


class Toggl:
    def __init__(self, settings: config.Settings | None = None):
        s = settings or config.load()
        self.token = config.secret(s.sources.toggl_token_env)
        self._projects: dict[int, str] | None = None

    def enabled(self) -> bool:
        return bool(self.token)

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
        e = self._get("/me/time_entries/current")
        return self._shape(e) if e else None

    def entries(self, days: int = 7) -> list[dict[str, Any]]:
        days = max(1, min(days, 90))
        end = dt.date.today() + dt.timedelta(days=1)
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
