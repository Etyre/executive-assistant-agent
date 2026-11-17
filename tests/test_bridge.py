import importlib.util
import pathlib
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("EA_BRIDGE_TOKEN", "brtok")
    spec = importlib.util.spec_from_file_location("mac_bridge", ROOT / "bridge" / "mac_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mac_bridge"] = mod
    spec.loader.exec_module(mod)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), dict(request.headers)))
        return httpx.Response(200, json={"echo": request.url.path})

    mod.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mod.seen = seen
    return mod


def test_bridge_auth_and_allow_lists(bridge):
    c = TestClient(bridge.app)
    H = {"X-Bridge-Token": "brtok"}
    assert c.get("/tracker/api/tree").status_code == 401
    assert c.get("/tracker/api/tree", headers={"X-Bridge-Token": "nope"}).status_code == 401
    assert c.get("/tracker/api/tree", headers={"Authorization": "Bearer brtok"}).status_code == 401   # that header belongs to the tracker
    assert c.get("/tracker/api/tree?active=1", headers=H).json() == {"echo": "/api/tree"}
    assert c.get("/focus/api/metrics?days=3", headers=H).status_code == 200
    assert c.get("/bandit/api/agent/summary", headers=H).status_code == 200
    assert c.get("/nothing/api/x", headers=H).status_code == 404
    # writes: tracker allow-list only
    assert c.put("/tracker/api/agent/schedule", headers=H, json={"items": []}).status_code == 200
    assert c.put("/tracker/api/agent/schedule/instructions", headers=H, json={"text": "x"}).status_code == 200
    assert c.delete("/tracker/api/agent/schedule/instructions", headers=H).status_code == 200
    assert c.post("/tracker/api/agent/quick_add", headers=H, json={"text": "x"}).status_code == 200
    assert c.post("/tracker/api/agent/notes", headers=H, json={"body": "x"}).status_code == 200
    assert c.delete("/tracker/api/agent/notes/3", headers=H).status_code == 200
    assert c.post("/tracker/api/tasks/5/complete", headers=H).status_code == 200
    assert c.patch("/tracker/api/agent/tasks/5", headers=H, json={"tags": "x"}).status_code == 200
    assert c.patch("/tracker/api/agent/projects/2", headers=H, json={"status": "paused"}).status_code == 200
    assert c.post("/tracker/api/checklist/done", headers=H, json={}).status_code == 403          # the checklist is read-only for the agent
    assert c.put("/tracker/api/plan", headers=H, json={"items": []}).status_code == 403          # the old route is not allowed
    assert c.delete("/tracker/api/tasks/5", headers=H).status_code == 403
    assert c.delete("/bandit/api/interventions/1", headers=H).status_code == 403
    assert c.post("/tracker/api/settings/secret", headers=H, json={}).status_code == 403
    assert c.post("/tracker/api/feed/start", headers=H, json={}).status_code == 403
    assert c.post("/focus/api/restart", headers=H).status_code == 403
    assert c.post("/bandit/api/cell", headers=H, json={}).status_code == 403
    # a GET with side effects upstream is blocked
    assert c.get("/focus/api/questions/queue", headers=H).status_code == 403
    assert bridge.seen[0][1].startswith("http://127.0.0.1:8791/api/tree")


def test_bridge_passes_the_agents_name_and_the_trackers_token_through(bridge):
    c = TestClient(bridge.app)
    c.post("/tracker/api/agent/notes", headers={"X-Bridge-Token": "brtok", "X-Agent-Name": "Atlas", "Authorization": "Bearer trk"}, json={"body": "x"})
    sent = bridge.seen[-1][2]
    assert sent["x-agent-name"] == "Atlas" and sent["authorization"] == "Bearer trk" and "x-bridge-token" not in sent


def test_every_write_the_tracker_connector_makes_is_allowed_through_the_bridge(bridge):
    """The connector and the bridge's allow-list are kept in step by this test, not by memory."""
    import re
    src = (ROOT / "ea/connectors/project_tracker.py").read_text()
    calls = re.findall(r'send_json\(c, "([A-Z]+)", f?"([^"]+)"', src)
    assert len(calls) >= 14
    for method, path in calls:
        path = re.sub(r"\{'uncomplete' if undo else 'complete'\}", "complete", path)
        path = re.sub(r"\{\w+\}", "7", path)
        assert bridge._allowed(method, "tracker", path), f"{method} {path} would be refused by the bridge"


def test_open_tracker_and_inbox(bridge, tmp_path, monkeypatch):
    c = TestClient(bridge.app)
    H = {"X-Bridge-Token": "brtok"}
    opened = []
    app = tmp_path / "Project Tracker.app"; app.mkdir()
    monkeypatch.setattr(bridge, "TRACKER_APP", app)
    monkeypatch.setattr(bridge.subprocess, "run", lambda cmd, **k: opened.append(cmd))
    assert c.post("/mac/open-tracker").status_code == 401 and opened == []
    assert c.post("/mac/open-tracker", headers=H).json()["ok"] and opened == [["open", "-g", str(app)]]
    monkeypatch.setattr(bridge, "INBOX", tmp_path / "inbox" / "messages.jsonl")
    rec = {"type": "contact", "id": "20261117-101010-ab12", "app": "executive-assistant", "model": "m", "message": "hello"}
    assert c.post("/inbox", json=rec).status_code == 401
    assert c.post("/inbox", headers=H, json={"type": "status", "id": "20261117-101010-ab12"}).status_code == 400
    assert c.post("/inbox", headers=H, json=rec).json() == {"ok": True, "id": rec["id"]}
    assert c.post("/inbox", headers=H, json=rec).json()["already"] is True                       # sent twice, stored once
    assert len(bridge.INBOX.read_text().splitlines()) == 1


def test_inbox_relay_sends_each_record_once_and_waits_for_the_mac(tmp_path, monkeypatch):
    import json
    from ea import contact, inbox_relay
    from ea.connectors.http import SourceError
    path = tmp_path / "messages.jsonl"
    monkeypatch.setattr(contact.llm_contact, "MESSAGES_PATH", path)
    lines = [{"type": "contact", "id": "a-000001"}, {"type": "status", "id": "a-000001", "status": "handled"}, {"type": "contact", "id": "a-000002"}]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))

    class Router:
        bridge, got, up = "http://100.1.1.1:8799", [], False
        def mac(self, method, p, body=None):
            if not self.up:
                raise SourceError("Mac away")
            self.got.append(body["id"])
    r = Router()
    assert inbox_relay.forward(router=r) == 0 and r.got == []            # Mac asleep: nothing lost, nothing marked
    r.up = True
    assert inbox_relay.forward(router=r) == 2 and r.got == ["a-000001", "a-000002"]
    assert inbox_relay.forward(router=r) == 0                            # nothing new
    with open(path, "a") as f:
        f.write(json.dumps({"type": "contact", "id": "a-000003"}) + "\n")
    assert inbox_relay.forward(router=r) == 1 and r.got[-1] == "a-000003"
    r.bridge = ""
    assert inbox_relay.forward(router=r) == 0                            # on the Mac itself: no relay


def test_through_the_bridge_an_unreachable_mac_is_said_in_plain_words():
    from ea.connectors import http as h
    def refuse(request):
        raise httpx.ConnectError("no route")
    c = httpx.Client(base_url="http://100.1.1.1:8799/tracker", headers={"X-Bridge-Token": "t"}, transport=httpx.MockTransport(refuse))
    with pytest.raises(h.SourceError, match="Mac is not reachable"):
        h.get_json(c, "/api/agent")
    local = httpx.Client(base_url="http://127.0.0.1:8791", transport=httpx.MockTransport(refuse))
    with pytest.raises(h.SourceError) as e:
        h.get_json(local, "/api/agent")
    assert "Mac is not reachable" not in str(e.value)


def test_config_overlay_changes_only_what_it_names():
    from ea import config
    base = {"timezone": "x", "sources": {"bridge_url": "", "timeout_seconds": 20}, "document_roots": [{"name": "a", "path": "/mac"}]}
    out = config._overlay(base, {"sources": {"bridge_url": "http://100.1.1.1:8799"}, "document_roots": [{"name": "a", "path": "/data/docs/a"}]})
    assert out["sources"] == {"bridge_url": "http://100.1.1.1:8799", "timeout_seconds": 20} and out["timezone"] == "x"
    assert out["document_roots"] == [{"name": "a", "path": "/data/docs/a"}] and base["sources"]["bridge_url"] == ""


def test_documents_are_listed_served_and_mirrored_with_their_exact_times(bridge, tmp_path, monkeypatch):
    """Bridge on the Mac -> copy on the server, through the real endpoints."""
    import os
    from ea import config, docs_mirror
    mac = tmp_path / "mac docs"; (mac / "sub").mkdir(parents=True)
    (mac / "a.md").write_text("alpha"); (mac / "sub" / "b.md").write_text("beta"); (mac / "note.txt").write_text("no"); (mac / ".hidden.md").write_text("no")
    os.utime(mac / "a.md", ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    secret = tmp_path / "secret.md"; secret.write_text("outside")
    monkeypatch.setattr(bridge, "_roots", lambda: {"site": mac, "gone": tmp_path / "nope"})
    c = TestClient(bridge.app)
    H = {"X-Bridge-Token": "brtok"}
    assert c.get("/documents").status_code == 401
    listing = c.get("/documents", headers=H).json()["roots"]
    assert [f["path"] for f in listing[0]["files"]] == ["a.md", "sub/b.md"] and listing[1] == {"name": "gone", "missing": True, "files": []}
    assert c.get("/documents/site/file", params={"path": "a.md"}, headers=H).text == "alpha"
    for bad in ("../secret.md", "note.txt", ".hidden.md", "missing.md"):
        assert c.get("/documents/site/file", params={"path": bad}, headers=H).status_code == 404

    class Router:                                            # the agent's router, wired straight to the bridge app
        bridge = "http://mac"
        def mac(self, method, path, body=None, raw=False, params=None):
            r = c.request(method, path, params=params, headers=H)
            assert r.status_code == 200
            return r.content if raw else r.json()
    copy = tmp_path / "server" / "site"; (tmp_path / "server" / "gone").mkdir(parents=True)
    (tmp_path / "server" / "gone" / "keep.md").write_text("kept: the Mac folder is missing, not empty")
    s = config.Settings()
    s.document_roots = [config.DocumentRoot("site", str(copy), from_mac=True), config.DocumentRoot("gone", str(tmp_path / "server" / "gone"), from_mac=True)]
    assert docs_mirror.sync(s, Router()) == {"fetched": 2, "removed": 0}
    assert (copy / "a.md").read_text() == "alpha" and (copy / "a.md").stat().st_mtime_ns == (mac / "a.md").stat().st_mtime_ns
    assert (tmp_path / "server" / "gone" / "keep.md").exists()
    assert docs_mirror.sync(s, Router()) == {"fetched": 0, "removed": 0}        # nothing changed: nothing fetched
    (mac / "a.md").write_text("alpha, edited"); (mac / "sub" / "b.md").unlink()
    assert docs_mirror.sync(s, Router()) == {"fetched": 1, "removed": 1}
    assert (copy / "a.md").read_text() == "alpha, edited" and not (copy / "sub" / "b.md").exists()

    class Away(Router):
        def mac(self, *a, **k):
            from ea.connectors.http import SourceError
            raise SourceError("Eli's Mac is not reachable")
    assert docs_mirror.sync(s, Away()) == {"fetched": 0, "removed": 0} and (copy / "a.md").exists()   # Mac asleep: the copy stays


def test_close_tracker_stops_launcher_then_python(bridge, tmp_path, monkeypatch):
    c = TestClient(bridge.app)
    H = {"X-Bridge-Token": "brtok"}
    app = tmp_path / "Project Tracker.app"
    monkeypatch.setattr(bridge, "TRACKER_APP", app)
    ps = f" 78 {app}/Contents/MacOS/ProjectTrackerApp -m tracker app\n 70 {app}/Contents/MacOS/ProjectTracker\n  1 /sbin/launchd\n"
    monkeypatch.setattr(bridge.subprocess, "run", lambda cmd, **k: type("R", (), {"stdout": ps})())
    killed = []
    monkeypatch.setattr(bridge.os, "kill", lambda pid, sig: killed.append(pid))
    assert c.post("/mac/close-tracker").status_code == 401 and killed == []
    assert c.post("/mac/close-tracker", headers=H).json()["ok"] and killed == [70, 78]
    ps = "  1 /sbin/launchd\n"
    assert c.post("/mac/close-tracker", headers=H).status_code == 404


def test_focus_monitor_answers_from_its_kept_copy_when_the_mac_is_away(tmp_path, monkeypatch):
    import httpx
    from ea import config
    from ea.connectors import focus_monitor as fm
    from ea.connectors.http import ServiceRouter, SourceError
    config.load.cache_clear()
    s = config.load(); s.sources.bridge_url = "http://mac:8799"
    state = {"up": True}

    def handler(req):
        if not state["up"]:
            raise httpx.ConnectError("no route to host")
        if req.url.path.endswith("/api/metrics"):
            return httpx.Response(200, json={"days": [{"day": "2026-11-16", "total_focus_min": 200}, {"day": "2026-11-17", "total_focus_min": 90}]})
        day = dict(req.url.params).get("day") or "2026-11-17"
        return httpx.Response(200, json={"day": day, "metrics": {"total_focus_min": 90}, "focus_spans": [{"start": "09:00", "duration": 1800}], "to_review": 2})

    router = ServiceRouter(s)
    monkeypatch.setattr(router, "client", lambda service: httpx.Client(base_url=f"http://mac:8799/{service}", transport=httpx.MockTransport(handler),
                                                                       headers={"X-Bridge-Token": "t"}))
    f = fm.FocusMonitor(router, cache_path=tmp_path / "focus_cache.json")
    assert f.refresh() >= 3                                            # today, yesterday and the metrics
    live_day, live_metrics = f.day(), f.metrics(2)
    state["up"] = False                                                # the Mac goes to sleep
    kept = f.day()
    assert "copy taken at" in kept.pop("note") and kept == live_day
    assert f.day("2026-11-16")["day"] == "2026-11-16"
    m = f.metrics(1)
    assert "copy taken at" in m["note"] and m["days"] == live_metrics[-1:]
    try:
        f.day("2026-10-01"); assert False
    except SourceError as e:
        assert "no copy" in str(e)
    assert f.refresh() == 0                                            # and a refresh while away keeps what it has
    assert f.day()["day"] == "2026-11-17"
    # on the Mac (no bridge) nothing is kept and errors pass straight through
    s.sources.bridge_url = ""
    assert fm.FocusMonitor(ServiceRouter(s)).cache_path is None
    config.load.cache_clear()


def test_a_direct_service_skips_the_bridge():
    from ea import config
    from ea.connectors.http import ServiceRouter
    config.load.cache_clear()
    s = config.load(); s.sources.bridge_url = "http://mac:8799"; s.sources.direct = ["bandit"]; s.sources.bandit_url = "http://bandit:8777"
    r = ServiceRouter(s); r.token = "t"
    assert str(r.client("bandit").base_url).rstrip("/") == "http://bandit:8777" and "x-bridge-token" not in r.client("bandit").headers
    assert str(r.client("focus").base_url).rstrip("/") == "http://mac:8799/focus" and r.via_bridge("tracker")
    config.load.cache_clear()
