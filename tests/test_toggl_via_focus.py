"""Toggl entries come from the Focus Monitor's synced table by default (2026-11-11), not from Toggl's API."""
import httpx

from ea import config
from ea.connectors import toggl as toggl_mod
from ea.connectors.http import ServiceRouter

ROWS = [{"id": 1, "description": "Deep work", "project": "Writing", "tags": [], "start": "2026-11-11T09:00:00-05:00",
         "stop": "2026-11-11T10:30:00-05:00", "minutes": 90, "running": False, "synced": "2026-11-11T10:31:00-05:00"},
        {"id": 2, "description": "Email", "project": None, "tags": ["admin"], "start": "2026-11-11T10:40:00-05:00",
         "stop": None, "minutes": 12, "running": True, "synced": "2026-11-11T10:52:00-05:00"}]


def test_reads_focus_monitor_not_toggl(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    s.sources.toggl_source = "focus_monitor"
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/api/toggl/current":
            return httpx.Response(200, json=ROWS[1])
        if request.url.path == "/api/toggl/entries":
            return httpx.Response(200, json=ROWS)
        return httpx.Response(404)
    monkeypatch.setattr(ServiceRouter, "client", lambda self, service: httpx.Client(base_url="http://focus", transport=httpx.MockTransport(handler)))
    t = toggl_mod.Toggl(s, ServiceRouter(s))
    assert t.enabled() and "Focus Monitor" in t.describe()
    assert t.current()["description"] == "Email" and t.current()["running"] is True
    assert [e["id"] for e in t.entries(7)] == [1, 2]
    summary = t.summary(7)
    assert summary["per_day"]["2026-11-11"] == {"Writing": 90, "(no project)": 12} and summary["total_minutes"] == 102
    assert all("focus" in c and "toggl.com" not in c for c in calls)   # never Toggl's API


def test_direct_toggl_still_available(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    s.sources.toggl_source = "toggl"
    monkeypatch.setenv("TOGGL_API_TOKEN", "")
    t = toggl_mod.Toggl(s)
    assert not t.enabled() and t.describe() == "Toggl API v9"
