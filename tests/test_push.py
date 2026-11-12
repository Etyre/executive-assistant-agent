"""Push channel (APNs) and the live event stream, with a fake Apple."""
import asyncio
import base64
import json
import pathlib
import tempfile

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from ea import config
from ea.channels.events import EventBus, Presence
from ea.channels.push import APNs, Devices, Push, make_jwt
from ea.channels.web import create_app
from tests.test_channels import EchoAgent

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


def _key():
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return k, pem


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_jwt_is_valid_es256():
    k, pem = _key()
    tok = make_jwt(pem, "KEYID12345", "TEAMID1234", now=1_700_000_000)
    h, c, sig = tok.split(".")
    assert json.loads(_b64d(h)) == {"alg": "ES256", "kid": "KEYID12345"}
    assert json.loads(_b64d(c)) == {"iss": "TEAMID1234", "iat": 1_700_000_000}
    raw = _b64d(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    k.public_key().verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))   # raises if wrong


def test_devices_store_roundtrip():
    db = pathlib.Path(tempfile.mkdtemp()) / "state.db"
    d = Devices(db)
    d.register(TOKEN_A, "ios", "Eli's iPhone", "sandbox")
    d.register(TOKEN_A, "ios", "Eli's iPhone", "production")       # re-register = update
    d.register(TOKEN_B, "macos", "MacBook")
    assert [x["token"] for x in d.all()] == [TOKEN_A, TOKEN_B]
    assert d.get(TOKEN_A)["env"] == "production"
    assert d.remove(TOKEN_B) and not d.remove(TOKEN_B)
    for bad in ("", "not-hex", "abc"):
        try:
            d.register(bad, "ios")
            assert False, bad
        except ValueError:
            pass


class FakeApple:
    """httpx transport standing in for api.push.apple.com."""

    def __init__(self):
        self.calls = []
        self.gone = set()
        self.expire_first = False

    def transport(self):
        def handler(req: httpx.Request) -> httpx.Response:
            self.calls.append(req)
            tok = req.url.path.rsplit("/", 1)[1]
            if self.expire_first:
                self.expire_first = False
                return httpx.Response(403, json={"reason": "ExpiredProviderToken"})
            if tok in self.gone:
                return httpx.Response(410, json={"reason": "Unregistered", "timestamp": 1})
            return httpx.Response(200)
        return httpx.MockTransport(handler)


def _push(settings, apple: FakeApple):
    _, pem = _key()
    apns = APNs(pem, "KID", "TEAM", "com.example.atlas", transport=apple.transport())
    return Push(settings, apns=apns, devices=Devices(pathlib.Path(tempfile.mkdtemp()) / "state.db"))


def test_push_fans_out_and_forgets_dead_devices(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    apple = FakeApple()
    p = _push(s, apple)
    p.devices.register(TOKEN_A, "ios", "phone", "sandbox")
    p.devices.register(TOKEN_B, "macos", "mac", "production")
    apple.gone.add(TOKEN_B)
    res = p.send("Deadline tomorrow with nothing scheduled.")
    assert [(r["status"], r["platform"]) for r in res] == [(200, "ios"), (410, "macos")]
    assert "forgotten" in res[1]["reason"] and p.devices.get(TOKEN_B) is None
    # per-device environment picks the host; headers and payload as Apple specifies
    req_a, req_b = apple.calls
    assert req_a.url.host == "api.sandbox.push.apple.com" and req_b.url.host == "api.push.apple.com"
    assert req_a.headers["apns-topic"] == "com.example.atlas" and req_a.headers["apns-push-type"] == "alert"
    assert req_a.headers["authorization"].startswith("bearer ey")
    body = json.loads(req_a.content)
    assert body["aps"]["alert"] == {"title": s.agent_name, "body": "Deadline tomorrow with nothing scheduled."}
    assert body["aps"]["sound"] == "default" and body["kind"] == "message"


def test_push_retries_once_on_expired_provider_token():
    config.load.cache_clear()
    apple = FakeApple()
    apple.expire_first = True
    p = _push(config.load(), apple)
    p.devices.register(TOKEN_A, "ios")
    first = p.apns.token()
    res = p.send("hi", only=[TOKEN_A], collapse_id="reply")
    assert res[0]["status"] == 200 and len(apple.calls) == 2
    assert apple.calls[1].headers["authorization"] != f"bearer {first}" or p.apns.token() != first
    assert apple.calls[1].headers["apns-collapse-id"] == "reply"


def test_push_unconfigured_reports_why(monkeypatch):
    for k in ("APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID"):
        monkeypatch.delenv(k, raising=False)
    config.load.cache_clear()
    p = Push(config.load(), devices=Devices(pathlib.Path(tempfile.mkdtemp()) / "s.db"))
    assert not p.enabled() and "APNS_KEY_PATH" in p.reason_disabled
    p.devices.register(TOKEN_A, "ios")
    assert p.send("x")[0]["reason"] == "apns not configured"
    assert "1 device(s)" in p.describe()


def test_long_text_truncated_in_alert():
    config.load.cache_clear()
    p = _push(config.load(), FakeApple())
    body = p.payload("x" * 5000)["aps"]["alert"]["body"]
    assert len(body) == 3000 and body.endswith("…")


def test_device_endpoints_and_turn_pushes_reply_to_asking_device(monkeypatch):
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    s = config.load()
    apple = FakeApple()
    push = _push(s, apple)
    c = TestClient(create_app(EchoAgent(), s, push=push, bus=EventBus()))
    H = {"Authorization": "Bearer secret-token"}
    assert c.post("/api/devices", json={"token": TOKEN_A, "platform": "ios"}).status_code == 401
    r = c.post("/api/devices", json={"token": TOKEN_A, "platform": "ios", "name": "phone", "env": "sandbox"}, headers=H)
    assert r.status_code == 200 and r.json()["device"]["env"] == "sandbox"
    assert c.post("/api/devices", json={"token": "zz", "platform": "ios"}, headers=H).status_code == 400
    assert len(c.get("/api/devices", headers=H).json()["devices"]) == 1
    # a chat turn from that device: the reply is pushed back to it (collapsed), not to anyone else
    r = c.post("/api/message", json={"text": "hello", "channel": "app"}, headers={**H, "X-Device-Token": TOKEN_A})
    assert r.json()["reply"] == "[app/chat] hello"
    assert len(apple.calls) == 1 and json.loads(apple.calls[0].content)["kind"] == "reply"
    # unknown device header: nothing pushed
    c.post("/api/message", json={"text": "hello", "channel": "app"}, headers={**H, "X-Device-Token": TOKEN_B})
    assert len(apple.calls) == 1
    # test endpoint and removal
    assert c.post("/api/push/test", json={}, headers=H).json()["sent"] == 1
    assert c.delete(f"/api/devices/{TOKEN_A}", headers=H).json()["ok"] is True
    assert c.get("/health").json()["push"].startswith("configured")


def test_event_bus_delivers_across_threads():
    bus = EventBus()

    async def main():
        sid, q = bus.subscribe()
        assert bus.subscribers == 1
        await asyncio.to_thread(bus.publish, {"type": "message", "who": "it", "text": "ping", "channel": "proactive"})
        e = await asyncio.wait_for(q.get(), 2)
        assert e["text"] == "ping" and e["ts"]
        bus.unsubscribe(sid)
        assert bus.publish({"type": "x"}) == 0
    asyncio.run(main())


def test_events_stream_endpoint(monkeypatch):
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    s = config.load()
    bus = EventBus()
    push = _push(s, FakeApple())
    c = TestClient(create_app(EchoAgent(), s, push=push, bus=bus))
    H = {"Authorization": "Bearer secret-token"}
    with c.stream("GET", "/api/events", headers=H) as r:
        lines = r.iter_lines()
        hello = json.loads(next(lines))
        assert hello["type"] == "hello" and hello["agent"] == s.agent_name
        # a chat turn made elsewhere shows up on the stream, both halves
        c.post("/api/message", json={"text": "from the mac", "channel": "app", "client_id": "mac-1"}, headers=H)
        a, b = json.loads(next(lines)), json.loads(next(lines))
        assert (a["who"], a["text"], a["client_id"]) == ("me", "from the mac", "mac-1")
        assert (b["who"], b["text"]) == ("it", "[app/chat] from the mac")


def test_notify_endpoint_fans_out(monkeypatch):
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    s = config.load()
    bus = EventBus()
    apple = FakeApple()
    push = _push(s, apple)
    push.devices.register(TOKEN_A, "macos", "mac")
    sent = []

    class A(EchoAgent):
        def _send_outbound(self, text):            # the real Agent's hook, wired by __main__ to push + bus
            sent.append(text)
            bus.publish({"type": "message", "who": "it", "text": text, "channel": "proactive"})
            push.send_sync(text)
    c = TestClient(create_app(A(), s, push=push, bus=bus))
    H = {"Authorization": "Bearer secret-token"}
    assert c.post("/api/notify", json={"text": " "}, headers=H).status_code == 400
    with c.stream("GET", "/api/events", headers=H) as r:
        lines = r.iter_lines()
        next(lines)
        d = c.post("/api/notify", json={"text": "Lunch block starts in 10 minutes."}, headers=H).json()
        assert d["ok"] and d["listeners"] == 1 and d["devices"] == 1
        e = json.loads(next(lines))
        assert (e["who"], e["channel"], e["text"]) == ("it", "proactive", "Lunch block starts in 10 minutes.")
    assert sent == ["Lunch block starts in 10 minutes."] and len(apple.calls) == 1


def test_presence_signals_and_ttls():
    pr = Presence(active_ttl=60, typed_ttl=180)
    assert not pr.in_chat(now=1000)
    pr.report("mac", True, now=1000)
    assert pr.in_chat(now=1050) and not pr.in_chat(now=1061)
    pr.report("mac", True, now=1000)
    pr.report("mac", False)
    assert not pr.in_chat(now=1001)
    pr.typed(now=2000)
    assert pr.in_chat(now=2100) and not pr.in_chat(now=2100, typed=False) and not pr.in_chat(now=2181)
    assert pr.describe(now=2100)["seconds_since_typed"] == 100


def test_in_chat_makes_messages_quiet(monkeypatch):
    """Eli, 2026-11-06: no notification while he is in the chat; the message just shows up in the thread."""
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    s = config.load()
    bus, pr, apple = EventBus(), Presence(), FakeApple()
    push = _push(s, apple)
    push.devices.register(TOKEN_A, "ios", "phone")

    class A(EchoAgent):
        def _send_outbound(self, text):       # mirrors ea.__main__._outbound
            quiet = pr.in_chat()
            bus.publish({"type": "message", "who": "it", "text": text, "channel": "proactive", "quiet": quiet})
            if not quiet:
                push.send_sync(text)
    c = TestClient(create_app(A(), s, push=push, bus=bus, presence=pr))
    H = {"Authorization": "Bearer secret-token"}
    with c.stream("GET", "/api/events", headers=H) as r:
        lines = r.iter_lines(); next(lines)
        # nobody in the chat: notify -> loud event + phone push
        assert c.post("/api/notify", json={"text": "a"}, headers=H).json()["quiet"] is False
        assert json.loads(next(lines))["quiet"] is False and len(apple.calls) == 1
        # the Mac app says its chat is in front: quiet event, no push
        assert c.post("/api/presence", json={"client_id": "mac", "active": True}, headers=H).json()["in_chat"] is True
        assert c.post("/api/notify", json={"text": "b"}, headers=H).json()["quiet"] is True
        assert json.loads(next(lines))["quiet"] is True and len(apple.calls) == 1
        # a reply to the phone's own question while the Mac chat is in front: no push either
        c.post("/api/message", json={"text": "q", "channel": "app", "client_id": "phone"}, headers={**H, "X-Device-Token": TOKEN_A})
        me, it = json.loads(next(lines)), json.loads(next(lines))
        assert me["quiet"] is True and it["quiet"] is True and len(apple.calls) == 1
        # Mac chat gone from the front; typing counts for a while for notifications, but not for the reply push
        c.post("/api/presence", json={"client_id": "mac", "active": False}, headers=H)
        c.post("/api/message", json={"text": "q2", "channel": "app", "client_id": "phone"}, headers={**H, "X-Device-Token": TOKEN_A})
        next(lines); assert json.loads(next(lines))["quiet"] is True
        assert len(apple.calls) == 2 and json.loads(apple.calls[1].content)["kind"] == "reply"
        assert c.get("/health").json()["presence"]["in_chat"] is True


def test_activity_history_and_live_hook(tmp_path):
    """The chain-of-thought pane: stored turns come back as events, and a live turn emits the same shapes."""
    from ea.agent.conversations import ConversationStore
    st = ConversationStore(tmp_path / "s.db", tmp_path / "t")
    th = st.current_thread()
    st.append(th, "user", [{"type": "text", "text": "[heartbeat: 12:03]\nUnscheduled check-in."}], "scheduler")
    st.append(th, "assistant", [{"type": "thinking", "thinking": "Let me see what is running.", "signature": "x"},
                                {"type": "tool_use", "id": "tu1", "name": "toggl_current", "input": {}}], "scheduler")
    st.append(th, "user", [{"type": "tool_result", "tool_use_id": "tu1", "content": "null"}], "scheduler")
    st.append(th, "assistant", [{"type": "text", "text": "Nothing running; no action."},
                                {"type": "tool_use", "id": "tu2", "name": "no_action_needed", "input": {"reason": "quiet"}}], "scheduler")
    st.append(th, "user", [{"type": "tool_result", "tool_use_id": "tu2", "content": "noted: quiet", "is_error": False}], "scheduler")
    st.append(th, "assistant", [{"type": "text", "text": "(done)"}], "scheduler")
    ev = st.activity()
    kinds = [(e["kind"], e.get("name") or e.get("channel") or "") for e in ev]
    assert kinds == [("turn", "scheduler"), ("thinking", ""), ("tool_call", "toggl_current"), ("tool_result", "toggl_current"),
                     ("text", "scheduler"), ("tool_call", "no_action_needed"), ("tool_result", "no_action_needed"), ("text", "scheduler")]
    assert ev[0]["label"].startswith("[heartbeat: 12:03]") and ev[1]["text"] == "Let me see what is running."
    assert ev[4]["final"] is False and ev[-1]["final"] is True
    assert st.activity(limit=2) == ev[-2:]

    # the live hook on a real Agent with a scripted provider
    from ea.agent.core import Agent
    from ea.llm.base import LLMResponse
    from ea import config
    config.load.cache_clear()
    s = config.load()

    class P:
        name = "scripted"
        calls = 0
        def complete(self, **kw):
            P.calls += 1
            if P.calls == 1:
                return LLMResponse(content=[{"type": "thinking", "thinking": "hmm"}, {"type": "tool_use", "id": "t1", "name": "list_reminders", "input": {}}], stop_reason="tool_use")
            return LLMResponse(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")
    a = Agent(s, provider=P())
    got = []
    a.activity = got.append
    assert a.handle("hello", channel="app") == "ok"
    assert [e["kind"] for e in got] == ["turn", "thinking", "tool_call", "tool_result", "text"]
    assert got[0]["label"] == "hello" and got[3]["name"] == "list_reminders" and got[4]["final"] is True


def test_integrations_status_shape_and_run_endpoint(monkeypatch):
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    from ea import integrations
    rows = integrations.status(config.load())
    keys = {r["key"] for r in rows}
    assert {"llm", "tracker", "otter", "roam", "push", "twilio", "public_url"} <= keys
    for r in rows:
        assert r["state"] in ("ok", "partial", "missing", "off") and r["needs"] and r["how"] and r["owner"]
    tw = next(r for r in rows if r["key"] == "twilio")
    assert tw["state"] == "missing" and "TWILIO_ACCOUNT_SID" in tw["detail"]
    s = config.load()

    class A(EchoAgent):
        def run_check(self, name, prompt, kind="scheduled check"):
            return f"[{kind}:{name}] {prompt}"
    c = TestClient(create_app(A(), s, push=_push(s, FakeApple()), bus=EventBus()))
    H = {"Authorization": "Bearer secret-token"}
    assert c.post("/api/run", json={"text": " "}, headers=H).status_code == 400
    assert c.post("/api/run", json={"text": "build the record", "name": "integrations"}, headers=H).json()["result"] == "[task:integrations] build the record"
