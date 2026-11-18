"""Images in the chat: stored as files, sent to the model as base64 blocks, served back to the app."""
import base64
import json

from fastapi.testclient import TestClient

from ea import config
from ea.agent.core import Agent
from ea.channels.events import EventBus
from ea.channels.web import create_app
from ea.images import ImageStore
from ea.llm.base import LLMResponse

PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fe0d4a1a2a0000000049454e44ae426082")).decode()


def test_store_roundtrip(tmp_path):
    st = ImageStore(tmp_path / "img")
    ref = st.save(PNG, "image/png")
    assert ref["source"]["type"] == "file" and ref["source"]["name"].endswith(".png")
    raw, media = st.load(ref["source"]["name"])
    assert media == "image/png" and base64.b64encode(raw).decode() == PNG
    inflated = st.inflate(ref)
    assert inflated["source"] == {"type": "base64", "media_type": "image/png", "data": PNG}
    assert st.load("../etc/passwd") is None and st.load("nope.png") is None
    try:
        st.save(PNG, "image/tiff"); assert False
    except ValueError:
        pass


def test_turn_with_image_reaches_model_and_history(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    s.sources.otter_logseq_graph = ""
    seen = []

    class P:
        name = "scripted"
        def complete(self, system, messages, tools, max_tokens, **kw):
            seen.append(messages)
            return LLMResponse(content=[{"type": "text", "text": "a tiny red square"}], stop_reason="end_turn")
    a = Agent(s, provider=P())
    said = []
    a.on_message = said.append
    ref = a.images.save(PNG, "image/png")
    assert a.handle("what is this?", channel="app", images=[ref]) == "a tiny red square"
    user = [m for m in seen[0] if m["role"] == "user"][-1]
    assert [b["type"] for b in user["content"]] == ["image", "text"] and user["content"][0]["source"]["type"] == "base64"
    assert said[0]["images"] == [ref["source"]["name"]]
    last = a.convo.readable(5)[-2]
    assert last["text"] == "what is this?" and last["images"] == [ref["source"]["name"]]
    # stored on disk as a file reference, never base64 in the database
    row = a.convo.history(a.convo.current_thread(), 5)[-2]
    assert row["content"][0]["source"]["type"] == "file"


def test_message_endpoint_accepts_images_and_serves_them(monkeypatch):
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    s = config.load()
    s.sources.otter_logseq_graph = ""

    class P:
        name = "scripted"
        def complete(self, system, messages, tools, max_tokens, **kw):
            last = [m for m in messages if m["role"] == "user"][-1]        # the store is shared across tests
            n = sum(1 for b in last["content"] if b.get("type") == "image")
            return LLMResponse(content=[{"type": "text", "text": f"saw {n} image(s)"}], stop_reason="end_turn")
    a = Agent(s, provider=P())
    from tests.test_push import FakeApple, _push
    c = TestClient(create_app(a, s, push=_push(s, FakeApple()), bus=EventBus()))
    H = {"Authorization": "Bearer secret-token"}
    r = c.post("/api/message", json={"text": "", "channel": "app", "images": [{"media_type": "image/png", "data": PNG}]}, headers=H)
    assert r.json()["reply"] == "saw 1 image(s)"
    name = a.convo.readable(3)[-2]["images"][0]
    img = c.get(f"/api/images/{name}", headers=H)
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert c.get("/api/images/nope.png", headers=H).status_code == 404
    assert c.post("/api/message", json={"text": "x", "images": [{"media_type": "image/bmp", "data": PNG}]}, headers=H).status_code == 400


def test_a_screenshot_named_by_its_mac_path_is_fetched_and_attached(tmp_path):
    import pytest
    from ea.images import MAC_PATH, read_mac_image
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True); (home / "Documents").mkdir()
    shot = home / "Downloads" / "Screenshot 2026-11-18 at 12.44.21 AM.png"       # macOS puts U+202F before AM
    shot.write_bytes(base64.b64decode(PNG))
    (home / "Documents" / "secret.png").write_bytes(b"x")
    (home / "Downloads" / "notes.txt").write_text("x")
    typed = str(home / "Downloads" / "Screenshot 2026-11-18 at 12.44.21 AM.png")       # as it arrives when copied
    assert read_mac_image(typed, home) == (base64.b64decode(PNG), "image/png")
    for bad in (str(home / "Documents" / "secret.png"), str(home / "Downloads" / "notes.txt"),
                str(home / "Downloads" / ".." / "Documents" / "secret.png"), str(home / "Downloads" / "gone.png"), "relative.png"):
        with pytest.raises(ValueError):
            read_mac_image(bad, home)
    text = f"[Image #17] look at this [Image: source: /Users/eli/Downloads/Screenshot 2026-11-18 at 12.44.21 AM.png] and ~/Desktop/a b.JPG."
    assert [m.group(0) for m in MAC_PATH.finditer(text)] == ["/Users/eli/Downloads/Screenshot 2026-11-18 at 12.44.21 AM.png", "~/Desktop/a b.JPG"]

    class Router:                                                    # the server: files come through the bridge
        bridge = "http://mac:8799"
        def mac(self, method, path, raw=False, params=None):
            assert (method, path, raw) == ("GET", "/mac/image", True)
            if "gone" in params["path"]:
                raise RuntimeError("GET /mac/image -> 404: no such file on the Mac")
            return base64.b64decode(PNG)
    st = ImageStore(tmp_path / "img")
    blocks, note = st.from_paths("see /Users/eli/Downloads/first.png and /Users/eli/Downloads/gone.png", Router())
    assert len(blocks) == 1 and st.load(blocks[0]["source"]["name"])[1] == "image/png"
    assert "gone.png" in note and "could not be fetched" in note and "first.png" not in note
    assert st.from_paths("no paths here", Router()) == ([], "")


def test_the_bridge_serves_only_images_in_downloads_and_desktop(tmp_path, monkeypatch):
    import importlib.util, pathlib, sys
    monkeypatch.setenv("EA_BRIDGE_TOKEN", "brtok")
    spec = importlib.util.spec_from_file_location("mac_bridge_img", pathlib.Path(__file__).resolve().parents[1] / "bridge" / "mac_bridge.py")
    mod = importlib.util.module_from_spec(spec); sys.modules["mac_bridge_img"] = mod; spec.loader.exec_module(mod)
    (tmp_path / "Desktop").mkdir(); (tmp_path / "Desktop" / "s.png").write_bytes(base64.b64decode(PNG)); (tmp_path / "x.png").write_bytes(b"x")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    c = TestClient(mod.app)
    H = {"X-Bridge-Token": "brtok"}
    assert c.get("/mac/image", params={"path": str(tmp_path / "Desktop" / "s.png")}).status_code == 401
    r = c.get("/mac/image", params={"path": str(tmp_path / "Desktop" / "s.png")}, headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == base64.b64decode(PNG)
    assert c.get("/mac/image", params={"path": str(tmp_path / "x.png")}, headers=H).status_code == 404
    assert c.get("/mac/image", params={"path": "/etc/passwd"}, headers=H).status_code == 404
