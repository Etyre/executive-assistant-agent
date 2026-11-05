import base64
import hashlib
import hmac

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
        self.convo = type("C", (), {"get": lambda self, k, d=None: None, "set": lambda self, k, v: None})()
        self.router = type("R", (), {"describe": lambda self: "none"})()

    def handle(self, text, channel="chat", mode="chat", store_user=True):
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


def test_telegram_chunking():
    long = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = _chunks(long, 4000)
    assert all(len(p) <= 4000 for p in parts) and "".join(p.replace("\n", "") for p in parts) == long.replace("\n", "")
    assert _chunks("", 10) == ["(empty)"]
