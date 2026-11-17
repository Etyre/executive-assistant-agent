"""tracker_open: the agent starts the Project Tracker app when its API is not answering."""
import pathlib

from ea import config
from ea.connectors import project_tracker as pt
from ea.connectors.http import ServiceRouter, SourceError
from ea.tools.registry import ToolRegistry
from ea.tools import tracker_tools


def _tracker(tmp_path, monkeypatch, bridge=""):
    config.load.cache_clear()
    s = config.load()
    s.sources.bridge_url = bridge
    app = tmp_path / "Project Tracker.app"; app.mkdir()
    s.sources.tracker_app = str(app)
    t = pt.ProjectTracker(ServiceRouter(s), s)
    opened = []
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **k: opened.append(cmd))
    monkeypatch.setattr(pt.time, "sleep", lambda s: None)
    return t, app, opened


def test_launch_opens_the_app_and_waits_for_its_api(tmp_path, monkeypatch):
    t, app, opened = _tracker(tmp_path, monkeypatch)
    pings = iter([False, False, True])
    monkeypatch.setattr(t, "running", lambda: next(pings))
    out = t.launch()
    assert out["ok"] and out["status"] == "started"
    assert opened == [["open", "-g", str(app)]]            # -g: the window must not steal focus


def test_launch_is_a_no_op_when_running_and_honest_when_it_cannot(tmp_path, monkeypatch):
    t, app, opened = _tracker(tmp_path, monkeypatch)
    monkeypatch.setattr(t, "running", lambda: True)
    assert t.launch() == {"ok": True, "status": "already running"} and opened == []
    monkeypatch.setattr(t, "running", lambda: False)
    t.app_path = str(tmp_path / "missing.app")
    assert "not found" in t.launch()["error"] and opened == []
    t.app_path = str(app)
    monkeypatch.setattr(pt.time, "time", iter([0, 0, 100, 100]).__next__)
    out = t.launch(wait_seconds=5)
    assert not out["ok"] and "did not answer" in out["error"] and opened == [["open", "-g", str(app)]]


def test_launch_through_the_bridge_asks_the_mac_to_open_it(tmp_path, monkeypatch):
    t, app, opened = _tracker(tmp_path, monkeypatch, bridge="http://100.1.1.1:8799")
    asked = []
    monkeypatch.setattr(t.router, "mac", lambda method, path, body=None: asked.append((method, path)))
    pings = iter([False, False, True])
    monkeypatch.setattr(t, "running", lambda: next(pings))
    assert t.launch()["status"] == "started" and asked == [("POST", "/mac/open-tracker")] and opened == []
    def away(method, path, body=None):
        raise SourceError("Eli's Mac is not reachable")
    monkeypatch.setattr(t.router, "mac", away)
    monkeypatch.setattr(t, "running", lambda: False)
    assert "not reachable" in t.launch()["error"]


def test_tracker_tools_point_at_tracker_open_when_the_app_is_down(tmp_path, monkeypatch):
    config.load.cache_clear()
    s = config.load()
    s.sources.tracker_url = "http://127.0.0.1:1"            # nothing listens there
    reg = ToolRegistry()
    tracker_tools.register(reg, ServiceRouter(s), s)
    out, err = reg.dispatch("tracker_feed", {})
    assert not err and "tracker_open" in out and "not answering" in out
    assert "tracker_open" in reg.names()


def test_tag_and_metadata_tools_send_what_was_given_to_the_agent_api(tmp_path, monkeypatch):
    """An empty string and 0 are values (they clear a field); only what was not given is left out."""
    t, _, _ = _tracker(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(pt, "send_json", lambda c, method, path, body=None, **q: sent.append((method, path, body)) or {"ok": True})
    reg = ToolRegistry()
    monkeypatch.setattr(tracker_tools, "ProjectTracker", lambda router, settings=None: t)
    tracker_tools.register(reg, t.router, config.load())
    reg.dispatch("tracker_set_task_meta", {"task_id": 7, "tags": "#p1 #reading", "estimate_minutes": 0, "toggl_title": "", "why": "asked"})
    reg.dispatch("tracker_set_project_meta", {"project_id": 3, "subtitle": "", "toggl_project": "Writing", "why": "asked"})
    assert sent == [
        ("PATCH", "/api/agent/tasks/7", {"tags": "#p1 #reading", "estimate_minutes": 0, "toggl_title": "", "why": "asked"}),
        ("PATCH", "/api/agent/projects/3", {"subtitle": "", "toggl_project": "Writing", "why": "asked"}),
    ]
