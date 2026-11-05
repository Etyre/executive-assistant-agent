"""The HTTP surface: health, the message endpoint the iPhone Shortcut and web chat use (bearer token),
a one-file web chat for the laptop, and the Twilio voice webhooks."""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .. import config
from ..agent.core import Agent
from . import twilio_voice

log = logging.getLogger(__name__)

CHAT_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}</title><style>
:root{{--bg:#fff;--fg:#111;--me:#e8f0fe;--it:#f3f3f3;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--me:#1d2b4a;--it:#222;--line:#333}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,system-ui,sans-serif;display:flex;flex-direction:column;height:100vh}}
#log{{flex:1;overflow:auto;padding:16px}} .m{{max-width:46em;margin:0 auto 10px;padding:10px 14px;border-radius:12px;white-space:pre-wrap}}
.me{{background:var(--me)}} .it{{background:var(--it)}} form{{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--line)}}
textarea{{flex:1;font:inherit;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);resize:none}}
button{{font:inherit;padding:10px 16px;border-radius:10px;border:0;background:#2962ff;color:#fff}}
#tok{{font-size:12px;opacity:.6;padding:4px 16px}}
</style></head><body><div id="tok">token: <input id="token" placeholder="EA_WEB_TOKEN" size="30"></div><div id="log"></div>
<form id="f"><textarea id="t" rows="2" placeholder="Message {name}… (Enter to send, Shift+Enter for newline)"></textarea><button>Send</button></form>
<script>
const log=document.getElementById('log'),t=document.getElementById('t'),tok=document.getElementById('token');
try{{tok.value=localStorage.getItem('ea_token')||''}}catch(e){{}}
tok.onchange=()=>{{try{{localStorage.setItem('ea_token',tok.value)}}catch(e){{}}}};
function add(c,txt){{const d=document.createElement('div');d.className='m '+c;d.textContent=txt;log.appendChild(d);log.scrollTop=1e9;return d}}
async function send(){{const txt=t.value.trim();if(!txt)return;t.value='';add('me',txt);const w=add('it','…');
const r=await fetch('/api/message',{{method:'POST',headers:{{'Content-Type':'application/json','Authorization':'Bearer '+tok.value}},body:JSON.stringify({{text:txt,channel:'web'}})}});
w.textContent=r.ok?(await r.json()).reply:'error '+r.status+': '+await r.text();}}
document.getElementById('f').onsubmit=e=>{{e.preventDefault();send()}};
t.onkeydown=e=>{{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();send()}}}};
</script></body></html>"""


class MessageIn(BaseModel):
    text: str
    channel: str = "shortcut"


def create_app(agent: Agent, settings: config.Settings | None = None) -> FastAPI:
    s = settings or config.load()
    ch = s.channels
    app = FastAPI(title=f"{s.agent_name} executive assistant")
    web_token = config.secret(ch.web_token_env)
    twilio_token = config.secret(ch.twilio_auth_token_env)

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not web_token:
            raise HTTPException(503, f"{ch.web_token_env} is not set; refusing requests")
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not given or not secrets.compare_digest(given, web_token):
            raise HTTPException(401, "bad token")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "agent": s.agent_name, "provider": agent.provider.name, "model": s.llm.model,
                "sources": agent.router.describe()}

    @app.get("/", response_class=HTMLResponse)
    def chat_page() -> str:
        return CHAT_HTML.format(name=s.agent_name)

    @app.post("/api/message", dependencies=[Depends(require_token)])
    async def message(m: MessageIn) -> dict[str, Any]:
        """iPhone Shortcut / web chat: one message in, the reply out (plain text, speakable)."""
        if not m.text.strip():
            raise HTTPException(400, "empty message")
        reply = await asyncio.to_thread(agent.handle, m.text, m.channel, "chat")
        return {"reply": reply}

    # ------------------------------------------------------------------ Twilio voice
    async def _twilio_form(request: Request) -> dict[str, Any]:
        form = dict(await request.form())
        if not twilio_token:
            raise HTTPException(503, "Twilio not configured")
        url = str(request.url)
        if ch.public_base_url:                                   # behind a proxy the public URL is what Twilio signed
            url = ch.public_base_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not twilio_voice.valid_signature(twilio_token, url, form, request.headers.get("X-Twilio-Signature")):
            raise HTTPException(403, "bad Twilio signature")
        caller = form.get("From") if not request.query_params.get("outbound") else form.get("To")
        if ch.twilio_allowed_callers and caller not in ch.twilio_allowed_callers:
            log.warning("rejected call from %s", caller)
            raise HTTPException(403, "caller not allowed")
        return form

    @app.post("/twilio/voice")
    async def twilio_voice_start(request: Request) -> Response:
        await _twilio_form(request)
        greeting = f"{s.agent_name} here. Go ahead."
        if request.query_params.get("outbound"):
            pending = agent.convo.get("outbound_call_message") or "I called because something needed your attention."
            agent.convo.set("outbound_call_message", None)
            greeting = f"{s.agent_name} here. {pending}"
        return Response(twilio_voice.twiml_gather(greeting, "/twilio/gather", ch.twilio_voice), media_type="application/xml")

    @app.post("/twilio/gather")
    async def twilio_gather(request: Request) -> Response:
        form = await _twilio_form(request)
        heard = (form.get("SpeechResult") or "").strip()
        if not heard:
            return Response(twilio_voice.twiml_gather("I didn't catch that. Say it again, or say goodbye to hang up.",
                                                      "/twilio/gather", ch.twilio_voice), media_type="application/xml")
        if heard.lower().rstrip(".!") in {"goodbye", "bye", "hang up", "that's all", "thanks that's all", "end call"}:
            return Response(twilio_voice.twiml_say_hangup("Talk soon.", ch.twilio_voice), media_type="application/xml")
        reply = await asyncio.to_thread(agent.handle, heard, "voice", "voice")
        return Response(twilio_voice.twiml_gather(reply, "/twilio/gather", ch.twilio_voice), media_type="application/xml")

    return app
