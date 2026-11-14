"""The HTTP surface: health, the message endpoints the apps, the iPhone Shortcut and the web chat use (bearer
token), device registration for push notifications, a live event stream, a one-file web chat for the laptop,
and the Twilio voice webhooks."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import secrets
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from .. import config, usage
from ..agent.core import Agent
from . import twilio_voice
from .events import BUS, PRESENCE, EventBus, Presence
from .push import Push

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
#tok{{font-size:12px;opacity:.6;padding:4px 16px}} #live{{color:#999}} #live.on{{color:#2a2}} #live.off{{color:#e80}}
.st{{font-size:12px;opacity:.55;max-width:46em;margin:-4px auto 10px;padding:0 14px}}
.it.busy::after{{content:'▍';opacity:.5;animation:b 1s steps(2) infinite}} @keyframes b{{50%{{opacity:0}}}}
</style></head><body><div id="tok">token: <input id="token" placeholder="EA_WEB_TOKEN" size="30"> <span id="live" title="not connected">●</span> <button id="nb" type="button" style="display:none;font-size:12px;padding:2px 8px;background:#555">enable notifications</button></div><div id="log"></div>
<form id="f"><textarea id="t" rows="2" placeholder="Message {name}… (Enter to send, Shift+Enter for newline)"></textarea><button>Send</button></form>
<script>
const log=document.getElementById('log'),t=document.getElementById('t'),tok=document.getElementById('token');
try{{tok.value=localStorage.getItem('ea_token')||''}}catch(e){{}}
tok.onchange=()=>{{try{{localStorage.setItem('ea_token',tok.value)}}catch(e){{}};loadHistory()}};
// Drafts are never lost: the composer is saved on every keystroke and restored before the page is usable.
const DRAFT='ea_draft_chat';try{{t.value=localStorage.getItem(DRAFT)||''}}catch(e){{}}
t.oninput=()=>{{try{{if(t.value)localStorage.setItem(DRAFT,t.value);else localStorage.removeItem(DRAFT)}}catch(e){{}}}};
function draftClear(){{try{{localStorage.removeItem(DRAFT)}}catch(e){{}}}}
function draftRestore(txt){{if(!t.value){{t.value=txt;try{{localStorage.setItem(DRAFT,txt)}}catch(e){{}}}}}}
let CID='';try{{CID=localStorage.getItem('ea_cid')||'';if(!CID){{CID='web-'+Math.random().toString(36).slice(2);localStorage.setItem('ea_cid',CID)}}}}catch(e){{CID='web-'+Date.now()}}
const nb=document.getElementById('nb'),live=document.getElementById('live');
function notifyUI(){{if(!('Notification' in window))return;nb.style.display=Notification.permission==='default'?'':'none'}}
nb.onclick=()=>Notification.requestPermission().then(notifyUI);notifyUI();
function notify(txt){{if(!('Notification' in window)||Notification.permission!=='granted')return;if(!document.hidden&&document.hasFocus())return;
try{{const n=new Notification('{name}',{{body:txt.length>400?txt.slice(0,399)+'…':txt,tag:'ea-'+Date.now()}});n.onclick=()=>{{window.focus();n.close()}}}}catch(e){{}}}}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function inFront(){{return !document.hidden&&document.hasFocus()}}
let wasFront=null;async function presence(){{const f=inFront();if(f===wasFront&&!f)return;wasFront=f;if(!tok.value)return;
try{{await fetch('/api/presence',{{method:'POST',headers:{{'Content-Type':'application/json','Authorization':'Bearer '+tok.value}},body:JSON.stringify({{client_id:CID,active:f}})}})}}catch(e){{}}}}
window.addEventListener('focus',presence);window.addEventListener('blur',presence);document.addEventListener('visibilitychange',presence);setInterval(presence,30000);presence();
async function liveFeed(){{for(;;){{if(!tok.value){{await sleep(2000);continue}}
try{{const r=await fetch('/api/events',{{headers:{{'Authorization':'Bearer '+tok.value}}}});
if(!r.ok){{live.className='off';live.title='live feed: '+r.status;await sleep(5000);continue}}
const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
for(;;){{const {{value,done}}=await rd.read();if(done)break;buf+=dec.decode(value,{{stream:true}});
const lines=buf.split('\\n');buf=lines.pop();
for(const ln of lines){{if(!ln.trim())continue;let e;try{{e=JSON.parse(ln)}}catch(x){{continue}}
if(e.type==='hello'){{live.className='on';live.title='live: new messages appear instantly'}}
else if(e.type==='message'&&e.client_id!==CID){{if(e.channel==='proactive')stamp('sent by the agent at '+(e.ts||'').slice(11,16));add(e.who,e.text);if(e.who==='it'&&!e.quiet)notify(e.text)}}}}}}
}}catch(err){{}}live.className='off';live.title='reconnecting…';await sleep(3000)}}}}
liveFeed();
function add(c,txt){{const d=document.createElement('div');d.className='m '+c;d.textContent=txt;log.appendChild(d);log.scrollTop=1e9;return d}}
function stamp(txt){{const d=document.createElement('div');d.className='st';d.textContent=txt;log.appendChild(d)}}
async function loadHistory(){{if(!tok.value)return;log.textContent='';
try{{const r=await fetch('/api/history?limit=80',{{headers:{{'Authorization':'Bearer '+tok.value}}}});
if(!r.ok){{stamp('history: '+r.status+' '+(await r.text()).slice(0,120));return}}
let day='';for(const m of (await r.json()).messages){{const d=m.ts.slice(0,10);if(d!==day){{day=d;stamp(day)}}
if(m.channel==='proactive')stamp('sent by the agent at '+m.ts.slice(11,16));add(m.who,m.text)}}
log.scrollTop=1e9}}catch(e){{stamp('history: '+e)}}}}
loadHistory();
const NICE={{tracker:'the Project Tracker',focus:'the Focus Monitor',sleep:'Sleep Bandit',oura:'Oura',toggl:'Toggl',calendar:'the calendar',memory:'memory',exercise:'the exercise log',read:'a document',list:'documents'}};
function nice(n){{const k=n.split('_')[0];return 'looking at '+(NICE[k]||n.replace(/_/g,' '))}}
async function send(){{const txt=t.value.trim();if(!txt)return;t.value='';add('me',txt);
const w=add('it','');w.classList.add('busy');const st=document.createElement('div');st.className='st';log.appendChild(st);let got='';
try{{
const r=await fetch('/api/message/stream',{{method:'POST',headers:{{'Content-Type':'application/json','Authorization':'Bearer '+tok.value}},body:JSON.stringify({{text:txt,channel:'web',client_id:CID}})}});
if(!r.ok){{w.textContent='error '+r.status+': '+await r.text();draftRestore(txt);return}}
draftClear();
const rd=r.body.getReader(),dec=new TextDecoder();let buf='';
for(;;){{const {{value,done}}=await rd.read();if(done)break;buf+=dec.decode(value,{{stream:true}});
const lines=buf.split('\\n');buf=lines.pop();
for(const ln of lines){{if(!ln.trim())continue;const e=JSON.parse(ln);
if(e.type==='text'){{got+=e.text;w.textContent=got;st.textContent='';log.scrollTop=1e9}}
else if(e.type==='tool'){{st.textContent=nice(e.name)+'…';log.scrollTop=1e9}}
else if(e.type==='done'){{if(!got)w.textContent=e.reply;st.textContent=''}}
else if(e.type==='error'){{w.textContent=(got?got+'\\n\\n':'')+'error: '+e.error;st.textContent=''}}}}}}
}}catch(err){{w.textContent=(got?got+'\\n\\n':'')+'error: '+err;if(!got)draftRestore(txt)}}finally{{w.classList.remove('busy');st.remove()}}}}
document.getElementById('f').onsubmit=e=>{{e.preventDefault();send()}};
t.onkeydown=e=>{{if(e.key==='Enter'&&!e.shiftKey){{e.preventDefault();send()}}}};
</script></body></html>"""


class ImageIn(BaseModel):
    media_type: str                 # image/png | image/jpeg | image/gif | image/webp
    data: str                       # base64


class MessageIn(BaseModel):
    text: str = ""
    channel: str = "shortcut"
    client_id: str = ""             # the app's own id, echoed in events so it can ignore what it already shows
    images: list[ImageIn] = []


class DeviceIn(BaseModel):
    token: str                      # hex device token from APNs
    platform: str = "ios"           # ios | macos
    name: str = ""                  # "Eli's iPhone"
    env: str = "production"         # production | sandbox (debug builds from Xcode)


class NotifyIn(BaseModel):
    text: str


class RunIn(BaseModel):
    text: str
    name: str = "request"


class WorkspaceIn(BaseModel):
    text: str
    base_version: str = ""          # the version the editor started from; "" = overwrite regardless
    client_id: str = ""
    note: str = ""


class PresenceIn(BaseModel):
    client_id: str
    active: bool = True             # the chat is in front on this device (refreshed every 30 s while it is)


class PushTestIn(BaseModel):
    text: str = ""
    token: str = ""                 # one device, or empty for all


def create_app(agent: Agent, settings: config.Settings | None = None, push: Push | None = None, bus: EventBus | None = None,
               presence: Presence | None = None) -> FastAPI:
    s = settings or config.load()
    ch = s.channels
    app = FastAPI(title=f"{s.agent_name} executive assistant")
    web_token = config.secret(ch.web_token_env)
    twilio_token = config.secret(ch.twilio_auth_token_env)
    push = push or Push(s)
    bus = bus or BUS
    presence = presence or PRESENCE
    app.state.push, app.state.bus, app.state.presence = push, bus, presence
    # The real Agent publishes every chat message itself (so replies to mid-turn messages reach the apps too);
    # a simpler agent (tests) is published for by _finish_turn.
    agent_publishes = hasattr(agent, "on_message")
    if agent_publishes and getattr(agent, "on_message") is None:
        agent.on_message = lambda e: bus.publish({**e, "quiet": True if e.get("who") == "me" else presence.in_chat()})

    def _store_images(m: "MessageIn") -> list[dict[str, Any]]:
        store = getattr(agent, "images", None)
        if not m.images or store is None:
            return []
        if len(m.images) > 6:
            raise HTTPException(400, "at most 6 images per message")
        try:
            return [store.save(i.data, i.media_type) for i in m.images]
        except ValueError as e:
            raise HTTPException(400, str(e))

    def _submit(m: "MessageIn", images: list[dict[str, Any]] | None = None) -> bool:
        sub = getattr(agent, "submit", None)
        return bool(sub and sub(m.text, m.channel, m.client_id, images=images))


    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not web_token:
            raise HTTPException(503, f"{ch.web_token_env} is not set; refusing requests")
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not given or not secrets.compare_digest(given, web_token):
            raise HTTPException(401, "bad token")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "agent": s.agent_name, "provider": agent.provider.name, "model": s.llm.model,
                "sources": agent.router.describe(), "push": push.describe(), "listeners": bus.subscribers,
                "presence": presence.describe(),
                "usage_30_days_by_speed": usage.summary(agent.convo, 30) if hasattr(agent.convo, "usage_totals") else [],   # fast vs standard: tokens, estimated cost
                "engine": agent.sdk.describe() if getattr(agent, "sdk", None) else {"engine": agent.provider.name, "billing": "api"},
                "workers": [w.describe() for w in agent.workers.running()] if getattr(agent, "workers", None) else []}

    def _finish_turn(m: MessageIn, reply: str, device_token: str | None) -> None:
        """After a chat turn: tell the other open apps, and push the reply to the device that asked (so a phone
        that went to the lock screen while the agent thought still gets the answer; the app hides the banner
        when it is in front)."""
        if not agent_publishes:
            now = dt.datetime.now().isoformat(timespec="seconds")
            bus.publish({"type": "message", "who": "me", "text": m.text, "channel": m.channel, "client_id": m.client_id, "ts": now, "quiet": True})
            bus.publish({"type": "message", "who": "it", "text": reply, "channel": m.channel, "client_id": m.client_id, "ts": now, "quiet": presence.in_chat()})
        if device_token and push.devices.get(device_token) and not presence.in_chat(typed=False):
            push.send(reply, only=[device_token], kind="reply", collapse_id="reply")     # phone may have locked meanwhile

    @app.get("/", response_class=HTMLResponse)
    def chat_page() -> str:
        return CHAT_HTML.format(name=s.agent_name)

    @app.post("/api/message", dependencies=[Depends(require_token)])
    async def message(m: MessageIn, x_device_token: str | None = Header(default=None)) -> dict[str, Any]:
        """iPhone Shortcut / apps / web chat: one message in, the reply out (plain text, speakable)."""
        if not m.text.strip() and not m.images:
            raise HTTPException(400, "empty message")
        presence.typed()
        images = _store_images(m)
        if _submit(m, images):                                        # a turn is running: it will see this
            return {"reply": "", "queued": True}
        reply = await asyncio.to_thread(agent.handle, m.text, m.channel, "chat", client_id=m.client_id, images=images)
        _finish_turn(m, reply, x_device_token)
        return {"reply": reply}

    # ------------------------------------------------------------------ apps: devices, live events
    @app.post("/api/devices", dependencies=[Depends(require_token)])
    def register_device(d: DeviceIn) -> dict[str, Any]:
        """The iPhone/Mac app registers (or refreshes) its APNs token on every launch."""
        try:
            dev = push.devices.register(d.token, d.platform, d.name, d.env)
        except ValueError as e:
            raise HTTPException(400, str(e))
        log.info("device registered: %s (%s/%s)", d.name or d.token[:8], d.platform, d.env)
        return {"ok": True, "device": dev, "push": push.describe()}

    @app.get("/api/devices", dependencies=[Depends(require_token)])
    def list_devices() -> dict[str, Any]:
        return {"devices": push.devices.all(), "push": push.describe()}

    @app.delete("/api/devices/{token}", dependencies=[Depends(require_token)])
    def remove_device(token: str) -> dict[str, Any]:
        return {"ok": push.devices.remove(token)}

    @app.post("/api/run", dependencies=[Depends(require_token)])
    async def run_task(r: RunIn) -> dict[str, Any]:
        """Hand the agent a task to do on its own (a proactive turn, like a check): it is not shown in the chat as a
        message from the user, it runs in the server so the thinking pane shows it, and anything it wants to tell
        the user it sends with send_message."""
        if not r.text.strip():
            raise HTTPException(400, "empty task")
        out = await asyncio.to_thread(agent.run_check, r.name, r.text, "task")
        return {"result": out}

    # ------------------------------------------------------------------ shared workspace
    ws = getattr(agent, "workspace", None)
    docs = getattr(agent, "documents", None)
    if docs is not None:
        docs.on_change = lambda info: bus.publish({"type": "workspace", **info})
    elif ws is not None:
        ws.on_change = lambda info: bus.publish({"type": "workspace", **info})

    def _doc(name: str):
        from ..workspace import valid_name
        if docs is None:
            raise HTTPException(503, "no documents")
        try:
            d = docs.get(valid_name(name), create=False)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if d is None:
            raise HTTPException(404, f"no document {name!r}")
        return d

    @app.get("/api/docs", dependencies=[Depends(require_token)])
    def docs_list() -> dict[str, Any]:
        return {"documents": docs.names() if docs is not None else []}

    @app.get("/api/docs/{name}", dependencies=[Depends(require_token)])
    def docs_get(name: str, history: int = 0) -> dict[str, Any]:
        d = _doc(name)
        out = d.read()
        if history:
            out["history"] = d.history(min(history, 100))
        return out

    @app.put("/api/docs/{name}", dependencies=[Depends(require_token)])
    def docs_put(name: str, w: WorkspaceIn) -> dict[str, Any]:
        from ..workspace import Conflict, valid_name
        if docs is None:
            raise HTTPException(503, "no documents")
        try:
            d = docs.get(valid_name(name), default=w.text)      # creates on first save
            return d.write(w.text, by="user", base_version=w.base_version or None, note=w.note or "edited in the app", client_id=w.client_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Conflict as c:
            raise HTTPException(409, detail=c.current)

    @app.delete("/api/docs/{name}", dependencies=[Depends(require_token)])
    def docs_delete(name: str) -> dict[str, Any]:
        if docs is None:
            raise HTTPException(503, "no documents")
        try:
            return {"ok": docs.delete(name)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/docs/{name}/versions/{version}", dependencies=[Depends(require_token)])
    def docs_version(name: str, version: str) -> dict[str, Any]:
        text = _doc(name).version_text(version)
        if text is None:
            raise HTTPException(404, "no such version")
        return {"name": name, "version": version, "text": text}

    @app.get("/api/workspace", dependencies=[Depends(require_token)])
    def workspace_get(history: int = 0) -> dict[str, Any]:
        if ws is None:
            raise HTTPException(503, "no workspace")
        out = ws.read()
        if history:
            out["history"] = ws.history(min(history, 100))
        return out

    @app.put("/api/workspace", dependencies=[Depends(require_token)])
    def workspace_put(w: WorkspaceIn) -> dict[str, Any]:
        """Save the user's edit. 409 with the current document if it moved on since `base_version`."""
        from ..workspace import Conflict
        if ws is None:
            raise HTTPException(503, "no workspace")
        try:
            return ws.write(w.text, by="user", base_version=w.base_version or None, note=w.note or "edited in the app", client_id=w.client_id)
        except Conflict as c:
            raise HTTPException(409, detail=c.current)

    @app.get("/api/workspace/versions/{version}", dependencies=[Depends(require_token)])
    def workspace_version(version: str) -> dict[str, Any]:
        text = ws.version_text(version) if ws is not None else None
        if text is None:
            raise HTTPException(404, "no such version")
        return {"version": version, "text": text}

    @app.post("/api/presence", dependencies=[Depends(require_token)])
    def report_presence(pr: PresenceIn) -> dict[str, Any]:
        """An app or page says its chat is in front (or no longer is). While any is, the agent's messages are
        quiet: shown in the thread, no notification, no phone push."""
        presence.report(pr.client_id, pr.active)
        d = presence.describe()
        log.info("presence: %s %s -> in_chat=%s", pr.client_id[:8], "in front" if pr.active else "away", d["in_chat"])
        return d

    @app.get("/api/images/{name}", dependencies=[Depends(require_token)])
    def image(name: str) -> Response:
        store = getattr(agent, "images", None)
        loaded = store.load(name) if store is not None else None
        if loaded is None:
            raise HTTPException(404, "no such image")
        raw, media = loaded
        return Response(raw, media_type=media, headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/notify", dependencies=[Depends(require_token)])
    def notify(n: NotifyIn) -> dict[str, Any]:
        """Send the user a message as if the agent had (scripts, tests): push + live feed + history."""
        if not n.text.strip():
            raise HTTPException(400, "empty message")
        send = getattr(agent, "_send_outbound", None)
        if send is None:
            raise HTTPException(503, "agent has no outbound channel")
        send(n.text)
        return {"ok": True, "listeners": bus.subscribers, "devices": len(push.devices.all()), "quiet": presence.in_chat()}

    @app.post("/api/push/test", dependencies=[Depends(require_token)])
    def push_test(t: PushTestIn) -> dict[str, Any]:
        """Send a test notification (to one device or all) and report what Apple said."""
        if not push.enabled():
            raise HTTPException(503, f"push not configured: {push.reason_disabled}")
        text = t.text or f"Test notification from {s.agent_name}."
        results = push.send(text, only=[t.token] if t.token else None, kind="test")
        return {"results": results, "sent": sum(1 for r in results if r["status"] == 200)}

    @app.on_event("shutdown")
    async def _bye() -> None:
        """Tell every open app the server is going away, so they reconnect at once instead of waiting on a dead socket."""
        bus.publish({"type": "bye"})
        await asyncio.sleep(0.2)

    @app.get("/api/events", dependencies=[Depends(require_token)])
    async def events_stream() -> StreamingResponse:
        """Live feed for an open app: one JSON object per line. {"type":"hello"} first, then {"type":"message",
        "who":"me"|"it","text",...} for every message that passes through (proactive ones have channel
        "proactive"), {"type":"ping"} every 25 s of silence."""
        sid, q = bus.subscribe()
        log.info("events: client %s subscribed (%d open)", sid, bus.subscribers)

        async def gen() -> AsyncIterator[bytes]:
            why = "finished"
            try:
                yield (json.dumps({"type": "hello", "agent": s.agent_name, "ts": dt.datetime.now().isoformat(timespec="seconds"),
                                   "workers": [w.describe() for w in agent.workers.running()] if getattr(agent, "workers", None) else []}) + "\n").encode()
                while True:
                    try:
                        e = await asyncio.wait_for(q.get(), timeout=25)
                    except asyncio.TimeoutError:
                        e = {"type": "ping"}
                    yield (json.dumps(e, ensure_ascii=False) + "\n").encode()
                    if e.get("type") == "bye":
                        why = "server shutting down"
                        return
            except asyncio.CancelledError:
                why = "client disconnected"
                raise
            except BaseException as e:  # noqa: BLE001 - log, then let the server handle it
                why = f"{type(e).__name__}: {e}"
                raise
            finally:
                bus.unsubscribe(sid)
                log.info("events: client %s gone (%s; %d open)", sid, why, bus.subscribers)

        return StreamingResponse(gen(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/history", dependencies=[Depends(require_token)])
    def history(limit: int = 60) -> dict[str, Any]:
        """Recent conversation for the chat page: user messages and the agent's replies and sent messages, oldest first."""
        return {"messages": agent.convo.readable(max(1, min(limit, 500)))}

    @app.get("/api/activity", dependencies=[Depends(require_token)])
    def activity(limit: int = 300) -> dict[str, Any]:
        """The agent's chain of thought, stored: turns, thinking, tool calls/results, replies. Live continuation
        arrives on /api/events as {"type":"activity", "kind":...}."""
        return {"events": agent.convo.activity(max(1, min(limit, 2000)))}

    @app.post("/api/message/stream", dependencies=[Depends(require_token)])
    async def message_stream(m: MessageIn, x_device_token: str | None = Header(default=None)) -> StreamingResponse:
        """Web chat: the same turn as /api/message, but streamed as one JSON object per line while it runs:
        {"type":"text","text":...} as the model writes, {"type":"tool","name":...} as tools run, then
        {"type":"done","reply":...} or {"type":"error","error":...}."""
        if not m.text.strip() and not m.images:
            raise HTTPException(400, "empty message")
        presence.typed()
        images = _store_images(m)
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        if _submit(m, images):
            q.put_nowait({"type": "queued"})
            q.put_nowait({"type": "done", "reply": ""})

        def emit(e: dict[str, Any]) -> None:          # called from the agent's thread
            loop.call_soon_threadsafe(q.put_nowait, e)

        def run() -> None:
            try:
                reply = agent.handle(m.text, m.channel, "chat", on_event=emit, client_id=m.client_id, images=images)
                emit({"type": "done", "reply": reply})
                _finish_turn(m, reply, x_device_token)
            except Exception as e:  # noqa: BLE001 - the page shows it
                log.exception("streamed turn failed")
                emit({"type": "error", "error": f"{type(e).__name__}: {e}"})

        async def events() -> AsyncIterator[bytes]:
            task = asyncio.ensure_future(asyncio.to_thread(run)) if q.empty() else asyncio.ensure_future(asyncio.sleep(0))
            try:
                while True:
                    e = await q.get()
                    yield (json.dumps(e, ensure_ascii=False) + "\n").encode()
                    if e["type"] in ("done", "error"):
                        break
            finally:
                await task

        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
