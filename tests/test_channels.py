import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from ea import config
from ea.channels.telegram import _chunks
from ea.channels.twilio_voice import twiml_gather, twiml_say_hangup, valid_signature
from ea.channels.web import create_app


class EchoAgent:
    class provider:
        name = "echo"

    def __init__(self):
        self.settings = config.load()
        self.convo = type("C", (), {"get": lambda self, k, d=None: None, "set": lambda self, k, v: None,
                                    "readable": lambda self, limit=60: [{"ts": "2026-11-04T21:29", "who": "it", "channel": "proactive", "text": "hi"}][:limit]})()
        self.router = type("R", (), {"describe": lambda self: "none"})()

    def handle(self, text, channel="chat", mode="chat", store_user=True, on_event=None, client_id="", images=None):
        if on_event:
            on_event({"type": "tool", "name": "tracker_feed"})
            on_event({"type": "text", "text": f"[{channel}/{mode}] "})
            on_event({"type": "text", "text": text})
        return f"[{channel}/{mode}] {text}"


def _sign(token, url, form):
    payload = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def test_twilio_signature_roundtrip():
    form = {"From": "+15551234567", "SpeechResult": "hello there", "CallSid": "CA1"}
    url = "https://ea.example.com/twilio/gather"
    sig = _sign("tok", url, form)
    assert valid_signature("tok", url, form, sig)
    assert not valid_signature("tok", url, {**form, "SpeechResult": "bye"}, sig)
    assert not valid_signature("tok", url, form, None)


def test_twiml_escapes():
    x = twiml_gather("Tom & Jerry <3", "/twilio/gather", "Polly.Joanna")
    assert "Tom &amp; Jerry &lt;3" in x and 'action="/twilio/gather"' in x and 'input="speech"' in x
    assert "<Hangup/>" in twiml_say_hangup("bye", "Polly.Joanna")


def test_message_endpoint_requires_token(monkeypatch):
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twtok")
    config.load.cache_clear()
    s = config.load()
    s.channels.twilio_allowed_callers = ["+15550001111"]
    s.channels.public_base_url = "https://ea.example.com"
    c = TestClient(create_app(EchoAgent(), s))
    assert c.get("/health").json()["ok"] is True
    assert "<textarea" in c.get("/").text
    assert c.post("/api/message", json={"text": "hi"}).status_code == 401
    assert c.post("/api/message", json={"text": "hi"}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/api/message", json={"text": "hi", "channel": "shortcut"}, headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200 and r.json()["reply"] == "[shortcut/chat] hi"
    # Twilio: unsigned -> 403; signed but wrong caller -> 403; signed allowed caller -> TwiML with the voice-mode reply
    form = {"From": "+15550001111", "To": "+15559999999", "SpeechResult": "what's my schedule", "CallSid": "CA1"}
    assert c.post("/twilio/gather", data=form).status_code == 403
    bad = {**form, "From": "+15550002222"}
    assert c.post("/twilio/gather", data=bad, headers={"X-Twilio-Signature": _sign("twtok", "https://ea.example.com/twilio/gather", bad)}).status_code == 403
    r = c.post("/twilio/gather", data=form, headers={"X-Twilio-Signature": _sign("twtok", "https://ea.example.com/twilio/gather", form)})
    assert r.status_code == 200 and "[voice/voice] what&apos;s my schedule" in r.text or "[voice/voice] what's my schedule" in r.text
    assert "<Gather" in r.text
    start = {"From": "+15550001111", "To": "+15559999999", "CallSid": "CA1"}
    r = c.post("/twilio/voice", data=start, headers={"X-Twilio-Signature": _sign("twtok", "https://ea.example.com/twilio/voice", start)})
    assert r.status_code == 200 and "Go ahead" in r.text
    # A call the agent placed: Twilio's own number is From and the user is To, on every request of the call.
    out = {"From": "+15559999999", "To": "+15550001111", "SpeechResult": "go on", "CallSid": "CA2"}
    r = c.post("/twilio/gather", data=out, headers={"X-Twilio-Signature": _sign("twtok", "https://ea.example.com/twilio/gather", out)})
    assert r.status_code == 200 and "<Gather" in r.text


def test_telegram_chunking():
    long = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = _chunks(long, 4000)
    assert all(len(p) <= 4000 for p in parts) and "".join(p.replace("\n", "") for p in parts) == long.replace("\n", "")
    assert _chunks("", 10) == ["(empty)"]


def test_stream_endpoint_sends_one_json_event_per_line(monkeypatch):
    import json
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    c = TestClient(create_app(EchoAgent(), config.load()))
    assert c.post("/api/message/stream", json={"text": "hi"}).status_code == 401
    r = c.post("/api/message/stream", json={"text": "hi", "channel": "web"}, headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    assert events[0] == {"type": "tool", "name": "tracker_feed"}
    assert "".join(e["text"] for e in events if e["type"] == "text") == "[web/chat] hi"
    assert events[-1] == {"type": "done", "reply": "[web/chat] hi"}


def test_history_endpoint_is_token_protected(monkeypatch):
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    c = TestClient(create_app(EchoAgent(), config.load()))
    assert c.get("/api/history").status_code == 401
    r = c.get("/api/history?limit=5", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200 and r.json()["messages"][0]["text"] == "hi"
    assert "loadHistory" in c.get("/").text


def test_chat_page_script_parses(monkeypatch):
    """The page is a Python format string; one wrong escape breaks every feature of it silently (2026-11-04)."""
    import re
    import shutil
    import subprocess
    import pytest
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    html = TestClient(create_app(EchoAgent(), config.load())).get("/").text
    js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    for line in js.splitlines():                       # no string literal may contain a raw line break
        assert line.count("'") % 2 == 0 or "//" in line, f"unbalanced quotes: {line[:80]}"
    if not shutil.which("node"):
        pytest.skip("node not installed; quote check only")
    r = subprocess.run(["node", "--check", "--input-type=module", "-"], input=js, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_a_resent_message_is_not_run_twice(monkeypatch):
    """The phone resends a message when its request was cut before any reply arrived; the same message id must
    get the first run's reply, not a second turn (Eli saw every message answered twice, 2026-11-15)."""
    import json
    monkeypatch.setenv("EA_WEB_TOKEN", "t")
    config.load.cache_clear()
    agent = EchoAgent()
    runs = []
    inner = agent.handle
    agent.handle = lambda text, *a, **k: (runs.append(text), inner(text, *a, **k))[1]
    c = TestClient(create_app(agent, config.load()))
    h = {"Authorization": "Bearer t"}
    body = {"text": "only once", "channel": "app", "client_id": "phone", "message_id": "local-me-1"}
    first = [json.loads(l) for l in c.post("/api/message/stream", json=body, headers=h).text.splitlines()]
    again = [json.loads(l) for l in c.post("/api/message/stream", json=body, headers=h).text.splitlines()]
    assert first[-1] == {"type": "done", "reply": "[app/chat] only once"}
    assert again == [{"type": "duplicate"}, {"type": "done", "reply": "[app/chat] only once"}]
    assert c.post("/api/message", json=body, headers=h).json() == {"reply": "[app/chat] only once", "duplicate": True}
    assert runs == ["only once"]
    # a different id, or none (the Shortcut, the web page), is a new message even with the same words
    c.post("/api/message", json={**body, "message_id": "local-me-2"}, headers=h)
    c.post("/api/message", json={**body, "message_id": ""}, headers=h)
    c.post("/api/message", json={**body, "message_id": ""}, headers=h)
    assert len(runs) == 4


def test_health_says_whether_a_turn_is_running(monkeypatch):
    """apps/Atlas/build.sh waits on this before it swaps the Mac app, so a reply being streamed is not cut off."""
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    agent = EchoAgent()
    c = TestClient(create_app(agent, config.load()))
    assert c.get("/health").json()["busy"] is False
    agent.busy = True
    assert c.get("/health").json()["busy"] is True


def test_a_message_handed_to_a_running_turn_is_done_only_once_it_is_stored(monkeypatch):
    """The apps reload the history on "done"; told too early, they dropped the message from the screen (2026-11-16)."""
    import threading
    import time
    monkeypatch.setenv("EA_WEB_TOKEN", "secret-token")
    config.load.cache_clear()
    agent = EchoAgent()
    stored_at = []

    def submit(text, channel, client_id, images=None, stored=None):
        def later():
            time.sleep(0.4)
            stored_at.append(time.monotonic())
            stored.set()
        threading.Thread(target=later).start()
        return True                                            # a turn is running: it takes the message
    agent.submit = submit
    c = TestClient(create_app(agent, config.load()))
    h = {"Authorization": "Bearer secret-token"}
    r = c.post("/api/message/stream", json={"text": "hi", "channel": "app"}, headers=h)
    done_at = time.monotonic()
    assert [json.loads(l)["type"] for l in r.text.splitlines()] == ["queued", "done"]
    assert stored_at and done_at > stored_at[0]
    assert c.post("/api/message", json={"text": "hi again", "channel": "app"}, headers=h).json() == {"reply": "", "queued": True}
