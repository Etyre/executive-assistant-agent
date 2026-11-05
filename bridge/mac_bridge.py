"""Mac bridge: lets the cloud-hosted agent reach the three local servers on this Mac.

Runs on the Mac (launchd), listens on the Tailscale interface only, requires a bearer token, and forwards:
  /tracker/...  -> Project Tracker  127.0.0.1:8791   (GET, plus an allow-list of writes)
  /focus/...    -> Focus Monitor    127.0.0.1:8790   (GET only)
  /bandit/...   -> Sleep Bandit     127.0.0.1:8777   (GET only)
Nothing else. When the agent itself moves to the Mac mini, this file is not needed: set the direct URLs.

Run:  EA_BRIDGE_TOKEN=... python3 bridge/mac_bridge.py            (binds the Tailscale IPv4 by default)
      EA_BRIDGE_HOST=0.0.0.0 ...                                   (only behind a firewall you trust)
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
import sys

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response

log = logging.getLogger("bridge")
TOKEN = os.environ.get("EA_BRIDGE_TOKEN", "")
PORT = int(os.environ.get("EA_BRIDGE_PORT", "8799"))
UPSTREAMS = {
    "tracker": os.environ.get("EA_TRACKER_URL", "http://127.0.0.1:8791"),
    "focus": os.environ.get("EA_FOCUS_URL", "http://127.0.0.1:8790"),
    "bandit": os.environ.get("EA_BANDIT_URL", "http://127.0.0.1:8777"),
}
# Writes the agent may perform, and only on the tracker.
WRITE_ALLOW = [
    # the tracker's agent API (attributed writes)
    ("POST", re.compile(r"^/api/agent/(tasks|projects|quick_add|notes)$")),
    ("DELETE", re.compile(r"^/api/agent/notes/\d+$")),
    ("PUT", re.compile(r"^/api/agent/schedule(/instructions)?$")),
    ("DELETE", re.compile(r"^/api/agent/schedule/instructions$")),
    ("POST", re.compile(r"^/api/agent/schedule/(rebuild|unpin)$")),
    # plain API calls the agent API does not cover
    ("POST", re.compile(r"^/api/tasks/\d+/(complete|uncomplete)$")),
    ("PATCH", re.compile(r"^/api/tasks/\d+$")),
    ("POST", re.compile(r"^/api/checklist/done$")),
]
# Reads that have side effects upstream; blocked.
READ_DENY = [re.compile(r"^/api/questions/queue")]

app = FastAPI(title="EA Mac bridge")
client = httpx.AsyncClient(timeout=60)


def _tailscale_ip() -> str | None:
    for cmd in (["tailscale", "ip", "-4"], ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
            if out:
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


@app.api_route("/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(service: str, path: str, request: Request) -> Response:
    auth = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not TOKEN or not auth or not secrets.compare_digest(auth, TOKEN):
        raise HTTPException(401, "bad token")
    if service not in UPSTREAMS:
        raise HTTPException(404, "unknown service")
    full = "/" + path
    if not _allowed(request.method, service, full):
        raise HTTPException(403, f"{request.method} {full} is not allowed through the bridge")
    body = await request.body()
    try:
        r = await client.request(request.method, UPSTREAMS[service] + full, params=request.query_params, content=body,
                                 headers={"content-type": request.headers.get("content-type", "application/json")})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"{service} unreachable: {e}")
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


@app.get("/health")
def health() -> dict:
    return {"ok": True, "services": list(UPSTREAMS)}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        sys.exit("set EA_BRIDGE_TOKEN (e.g. `openssl rand -hex 32`)")
    host = os.environ.get("EA_BRIDGE_HOST") or _tailscale_ip()
    if not host:
        sys.exit("Tailscale IP not found; install Tailscale or set EA_BRIDGE_HOST explicitly")
    log.info("bridge on %s:%s -> %s", host, PORT, UPSTREAMS)
    uvicorn.run(app, host=host, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
