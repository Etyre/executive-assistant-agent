"""Mac bridge: lets the agent, when it runs on a server, reach what only exists on this Mac.

Runs on the Mac under launchd (com.elityre.ea-bridge, see install_bridge.sh), listens on the Tailscale address
only, and wants the token in an `X-Bridge-Token` header. It does these things and nothing else:

  /tracker/...       -> Project Tracker  127.0.0.1:8791   (GET, plus an allow-list of writes)
  /focus/...         -> Focus Monitor    127.0.0.1:8790   (GET only)
  /bandit/...        -> Sleep Bandit     127.0.0.1:8777   (GET only)
  POST /mac/open-tracker   starts the Project Tracker app when its API is down
  POST /mac/close-tracker  quits it (launcher and Python)
  POST /inbox              appends an LLM-inbox record written on the server to this Mac's inbox, so `llm-inbox`
                           and the page on :8798 show it (the promise in the shared preamble is kept from here)
  GET  /documents          the Markdown files in config.toml's [[document_roots]] (name, size, modified time), and
  GET  /documents/<root>/file?path=...   one of them; the server keeps a copy of each folder up to date from these

`Authorization` and `X-Agent-Name` are passed through to the tracker, which is why the bridge's own token is in a
header of its own. When the agent runs on this Mac the bridge is not used: `sources.bridge_url = ""`.

Token: EA_BRIDGE_TOKEN in the environment, else in <repo>/data/.env.   Log: ~/Library/Logs/ea-bridge.log
Run by hand:  .venv/bin/python bridge/mac_bridge.py        (EA_BRIDGE_HOST=127.0.0.1 to try it without Tailscale)
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import secrets
import signal
import subprocess
import sys
import tomllib

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response

log = logging.getLogger("bridge")
REPO = pathlib.Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("EA_BRIDGE_PORT", "8799"))
UPSTREAMS = {
    "tracker": os.environ.get("EA_TRACKER_URL", "http://127.0.0.1:8791"),
    "focus": os.environ.get("EA_FOCUS_URL", "http://127.0.0.1:8790"),
    "bandit": os.environ.get("EA_BANDIT_URL", "http://127.0.0.1:8777"),
}
TRACKER_APP = pathlib.Path(os.environ.get("EA_TRACKER_APP") or "~/Documents/agent-coding/project-tracker-and-organizer/dist/Project Tracker.app").expanduser()
INBOX = pathlib.Path(os.environ.get("LLM_INBOX_DIR") or pathlib.Path.home() / "Library/Application Support/LLMInbox") / "messages.jsonl"
# Every write the agent's tracker connector makes (ea/connectors/project_tracker.py); tests/test_bridge.py checks
# the two stay in step. The daily checklist is read-only for the agent, so it is not here.
WRITE_ALLOW = [
    ("POST", re.compile(r"^/api/agent/(tasks|projects|quick_add|notes)$")),
    ("PATCH", re.compile(r"^/api/agent/(tasks|projects)/\d+$")),
    ("DELETE", re.compile(r"^/api/agent/notes/\d+$")),
    ("PUT", re.compile(r"^/api/agent/schedule(/instructions)?$")),
    ("DELETE", re.compile(r"^/api/agent/schedule/instructions$")),
    ("POST", re.compile(r"^/api/agent/schedule/(rebuild|unpin)$")),
    ("POST", re.compile(r"^/api/tasks/\d+/(complete|uncomplete)$")),
    ("PATCH", re.compile(r"^/api/tasks/\d+$")),
]
# Reads that have side effects upstream; blocked.
READ_DENY = [re.compile(r"^/api/questions/queue")]
PASS_HEADERS = ("content-type", "authorization", "x-agent-name")

app = FastAPI(title="EA Mac bridge", docs_url=None, redoc_url=None, openapi_url=None)
client = httpx.AsyncClient(timeout=60)


def token() -> str:
    """Read on every request, so a token changed in data/.env takes effect without a restart."""
    t = os.environ.get("EA_BRIDGE_TOKEN", "")
    if t:
        return t
    try:
        for line in (REPO / "data/.env").read_text().splitlines():
            if line.startswith("EA_BRIDGE_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _tailscale_ip() -> str | None:
    """This Mac's Tailscale address: the 100.64.0.0/10 address on a utun interface. Read from ifconfig, because the
    App Store Tailscale's command line does not answer under launchd (seen 2026-11-17); the CLI is the fallback."""
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
        for ip in re.findall(r"^\s+inet (100\.\d+\.\d+\.\d+) ", out, flags=re.M):
            if 64 <= int(ip.split(".")[1]) <= 127:
                return ip
    except (OSError, subprocess.SubprocessError):
        pass
    for cmd in (["tailscale", "ip", "-4"], ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
            if out and out[0].startswith("100."):
                return out[0]
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _allowed(method: str, service: str, path: str) -> bool:
    if method == "GET":
        return not any(r.match(path) for r in READ_DENY)
    if service != "tracker":
        return False
    return any(m == method and r.match(path) for m, r in WRITE_ALLOW)


def _auth(request: Request) -> None:
    want, got = token(), request.headers.get("x-bridge-token", "")
    if not want or not got or not secrets.compare_digest(got, want):
        raise HTTPException(401, "bad token")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "services": list(UPSTREAMS)}


@app.post("/mac/open-tracker")
def open_tracker(request: Request) -> dict:
    _auth(request)
    if not TRACKER_APP.exists():
        raise HTTPException(404, f"tracker app not found at {TRACKER_APP}")
    subprocess.run(["open", "-g", str(TRACKER_APP)], check=True, timeout=15)     # -g: do not take Eli's focus
    log.info("opened %s", TRACKER_APP.name)
    return {"ok": True, "opened": TRACKER_APP.name}


@app.post("/mac/close-tracker")
def close_tracker(request: Request) -> dict:
    """Quit the tracker app: its launcher first (shortest command line), then the Python it would restart."""
    _auth(request)
    out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    found = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if cmd.strip().startswith(str(TRACKER_APP) + "/Contents/MacOS/"):
            found.append((len(cmd.strip()), int(pid)))
    if not found:
        raise HTTPException(404, "the tracker app is not running from its app bundle")
    for _, pid in sorted(found):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    log.info("quit %s (pids %s)", TRACKER_APP.name, [p for _, p in sorted(found)])
    return {"ok": True, "closed": TRACKER_APP.name}


@app.post("/inbox")
async def inbox(request: Request) -> dict:
    """One LLM-inbox record from the server. Appended once (by id); the server has already notified Eli."""
    _auth(request)
    rec = await request.json()
    if not isinstance(rec, dict) or rec.get("type") != "contact" or not re.fullmatch(r"[\w-]{6,40}", str(rec.get("id", ""))):
        raise HTTPException(400, "not an inbox contact record")
    INBOX.parent.mkdir(parents=True, exist_ok=True)
    if INBOX.exists():
        with open(INBOX) as f:
            for line in f:
                if rec["id"] in line and json.loads(line).get("id") == rec["id"] and json.loads(line).get("type") == "contact":
                    return {"ok": True, "id": rec["id"], "already": True}
    with open(INBOX, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log.warning("inbox record %s from %s (%s) received from the server", rec["id"], rec.get("app"), rec.get("model"))
    return {"ok": True, "id": rec["id"]}


def _roots() -> dict[str, pathlib.Path]:
    """The document folders named in this Mac's config.toml (read each time: a new root needs no restart)."""
    try:
        raw = tomllib.loads(pathlib.Path(os.environ.get("EA_CONFIG") or REPO / "config.toml").read_text())
    except (OSError, ValueError):
        return {}
    return {r["name"]: pathlib.Path(r["path"]).expanduser() for r in raw.get("document_roots", []) if r.get("name") and r.get("path")}


def _md_files(folder: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in folder.rglob("*.md") if p.is_file() and p.stem and not any(part.startswith(".") for part in p.relative_to(folder).parts))


@app.get("/documents")
def documents(request: Request) -> dict:
    _auth(request)
    out = []
    for name, folder in _roots().items():
        if not folder.is_dir():
            out.append({"name": name, "missing": True, "files": []})
            continue
        files = []
        for p in _md_files(folder):
            st = p.stat()
            files.append({"path": p.relative_to(folder).as_posix(), "size": st.st_size, "mtime_ns": st.st_mtime_ns})
        out.append({"name": name, "missing": False, "files": files})
    return {"roots": out}


@app.get("/documents/{name}/file")
def document_file(name: str, path: str, request: Request) -> Response:
    _auth(request)
    folder = _roots().get(name)
    if folder is None:
        raise HTTPException(404, "unknown document root")
    p = (folder / path).resolve()
    if not p.is_relative_to(folder.resolve()) or p not in [f.resolve() for f in _md_files(folder)]:
        raise HTTPException(404, "no such document")           # only what /documents lists; nothing outside the folder
    return Response(p.read_bytes(), media_type="text/markdown")


@app.api_route("/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(service: str, path: str, request: Request) -> Response:
    _auth(request)
    if service not in UPSTREAMS:
        raise HTTPException(404, "unknown service")
    full = "/" + path
    if not _allowed(request.method, service, full):
        raise HTTPException(403, f"{request.method} {full} is not allowed through the bridge")
    headers = {h: request.headers[h] for h in PASS_HEADERS if h in request.headers}
    try:
        r = await client.request(request.method, UPSTREAMS[service] + full, params=request.query_params,
                                 content=await request.body(), headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"{service} is not answering on the Mac: {e}")
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not token():
        sys.exit("no EA_BRIDGE_TOKEN in the environment or data/.env (bridge/install_bridge.sh writes one)")
    host = os.environ.get("EA_BRIDGE_HOST") or _tailscale_ip()
    if not host:
        sys.exit("Tailscale address not found (is Tailscale connected?); launchd will try again")
    log.info("bridge on %s:%s -> %s", host, PORT, UPSTREAMS)
    uvicorn.run(app, host=host, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
