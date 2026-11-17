"""The shared workspace: a Markdown page the user and the agent both edit (Eli, 2026-11-06)."""
import pathlib
import tempfile

from fastapi.testclient import TestClient

from ea import config
from ea.channels.events import EventBus
from ea.channels.web import create_app
from ea.workspace import Conflict, Workspace
from tests.test_channels import EchoAgent
from tests.test_push import FakeApple, _push


def _ws():
    d = pathlib.Path(tempfile.mkdtemp())
    config.load.cache_clear()
    return Workspace(config.load(), path=d / "workspace.md", db_path=d / "state.db")


def test_edit_append_write_and_conflict():
    ws = _ws()
    first = ws.read()
    assert first["text"].startswith("# Workspace") and first["updated_by"] == "system"
    out = ws.append("## Plan for Friday\n- call the carpet guy", by="agent", note="plan")
    assert out["text"].endswith("- call the carpet guy\n") and out["updated_by"] == "agent"
    out = ws.edit("call the carpet guy", "call the carpet guy (done)", by="user")
    assert "(done)" in out["text"]
    try:
        ws.edit("the", "X", by="agent")                  # ambiguous: occurs more than once
        assert False, "ambiguous edit accepted"
    except ValueError:
        pass
    try:
        ws.edit("not in the page", "X", by="agent")
        assert False
    except ValueError:
        pass
    stale = first["version"]
    try:
        ws.write("rewrite", by="agent", base_version=stale)
        assert False, "stale write accepted"
    except Conflict as c:
        assert "(done)" in c.current["text"]
    ok = ws.write("rewrite\n", by="agent", base_version=out["version"])
    assert ok["text"] == "rewrite\n" and ws.read()["version"] == ok["version"]
    hist = ws.history()
    assert [h["by"] for h in hist][:3] == ["agent", "user", "agent"] and ws.version_text(stale).startswith("# Workspace")
    # an edit made outside the app is seen as a new version
    ws.path.write_text("edited in Literal\n")
    assert ws.read()["updated_by"] == "outside the app"


def test_workspace_endpoints_and_live_event(monkeypatch):
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    s = config.load()
    ws = _ws()
    changes = []
    bus = EventBus()

    class A(EchoAgent):
        workspace = ws
    c = TestClient(create_app(A(), s, push=_push(s, FakeApple()), bus=bus))
    H = {"Authorization": "Bearer secret-token"}
    base = c.get("/api/workspace", headers=H).json()
    with c.stream("GET", "/api/events", headers=H) as r:
        lines = r.iter_lines(); next(lines)
        saved = c.put("/api/workspace", json={"text": "hello\n", "base_version": base["version"], "client_id": "mac"}, headers=H).json()
        assert saved["updated_by"] == "user" and saved["text"] == "hello\n"
        import json
        e = json.loads(next(lines))
        assert e["type"] == "workspace" and e["by"] == "user" and e["client_id"] == "mac" and e["version"] == saved["version"]
        # a stale save is refused with the current document
        r2 = c.put("/api/workspace", json={"text": "stale\n", "base_version": base["version"]}, headers=H)
        assert r2.status_code == 409 and r2.json()["detail"]["text"] == "hello\n"
        # the agent's tool edit shows up as an event too
        ws.append("- from the agent", by="agent")
        e2 = json.loads(next(lines))
        assert e2["by"] == "agent"
    assert c.get(f"/api/workspace/versions/{base['version']}", headers=H).json()["text"] == base["text"]
    assert c.get("/api/workspace?history=5", headers=H).json()["history"][0]["by"] == "agent"


def test_remove_tool_prunes_cleanly():
    from ea.tools.registry import ToolRegistry
    from ea.tools import workspace_tools
    ws = _ws()
    ws.write("# Workspace\n\n## Today\n- call the carpet guy\n- send the bill specs\n\n## Later\n- taxes\n", by="user")
    reg = ToolRegistry()
    shown = []
    workspace_tools.register(reg, ws, show=lambda name='workspace': shown.append(1))
    out, err = reg.dispatch("workspace_remove", {"find": "- call the carpet guy\n", "note": "done"})
    assert not err and ws.read()["text"] == "# Workspace\n\n## Today\n- send the bill specs\n\n## Later\n- taxes\n"
    out, err = reg.dispatch("workspace_remove", {"find": "## Later\n- taxes\n"})
    assert ws.read()["text"] == "# Workspace\n\n## Today\n- send the bill specs\n" and shown == [1, 1]
    assert "occurs 0 times" in reg.dispatch("workspace_remove", {"find": "nope"})[0]
    assert ws.history()[0]["note"] == "removed" and ws.history()[1]["note"] == "done"


def test_documents_store_and_endpoints(monkeypatch):
    import json, tempfile
    from ea.workspace import Documents
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    s = config.load()
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "workspace.md").write_text("# old single file\n")         # pre-multi-document layout migrates
    docs = Documents(s, folder=d / "workspace", db_path=d / "state.db")
    assert docs.whiteboard().read()["text"] == "# old single file\n" and not (d / "workspace.md").exists()
    plan = docs.get("DC trip plan", default="# DC trip\n\n```mermaid\ngantt\n  title Trip\n```\n")
    assert [x["name"] for x in docs.names()] == ["workspace", "DC trip plan"] and docs.names()[1]["title"] == "DC trip"
    plan.append("- book flights", by="agent")
    assert plan.history()[0]["by"] == "agent" and docs.whiteboard().history()[0]["by"] == "system"   # histories are per document
    try:
        docs.get("../evil"); assert False
    except ValueError:
        pass
    try:
        docs.delete("workspace"); assert False
    except ValueError:
        pass
    events = []
    docs.on_change = events.append
    assert docs.delete("DC trip plan") and docs.get("DC trip plan", create=False) is None and events[-1]["deleted"]

    class A(EchoAgent):
        documents = docs
        workspace = docs.whiteboard()
    c = TestClient(create_app(A(), s, push=_push(s, FakeApple()), bus=EventBus()))
    H = {"Authorization": "Bearer secret-token"}
    assert c.put("/api/docs/Notes", json={"text": "# Notes\n- a\n"}, headers=H).json()["name"] == "Notes"
    assert [x["name"] for x in c.get("/api/docs", headers=H).json()["documents"]] == ["workspace", "Notes"]
    assert c.get("/api/docs/Notes", headers=H).json()["text"] == "# Notes\n- a\n"
    assert c.get("/api/docs/nope", headers=H).status_code == 404 and c.put("/api/docs/..x", json={"text": "y"}, headers=H).status_code == 400
    assert c.get("/api/workspace", headers=H).json()["name"] == "workspace"            # the old endpoint still serves the whiteboard
    assert c.delete("/api/docs/Notes", headers=H).json()["ok"] is True
    assert c.delete("/api/docs/workspace", headers=H).status_code == 400


def test_documents_can_be_read_from_many_threads_at_once():
    """All documents share one database connection and the web server reads from several threads: two overlapping
    reads raised sqlite3.InterfaceError and the app's document list came back as a 500 (2026-11-16)."""
    import tempfile, threading
    from ea.workspace import Documents
    config.load.cache_clear()
    d = pathlib.Path(tempfile.mkdtemp())
    docs = Documents(config.load(), folder=d / "workspace", db_path=d / "state.db")
    for i in range(6):
        docs.get(f"Doc {i} - shareable").write("# doc\n\n" + "x" * 2000 * (i + 1), by="agent")
    errors = []

    def hammer():
        try:
            for _ in range(60):
                assert len(docs.names()) == 6
                docs.get("Doc 3 - shareable").history()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
    threads = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
