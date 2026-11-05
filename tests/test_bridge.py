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
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json={"echo": request.url.path})

    mod.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mod.seen = seen
    return mod


def test_bridge_auth_and_allow_lists(bridge):
    c = TestClient(bridge.app)
    H = {"Authorization": "Bearer brtok"}
    assert c.get("/tracker/api/tree").status_code == 401
    assert c.get("/tracker/api/tree", headers={"Authorization": "Bearer nope"}).status_code == 401
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
