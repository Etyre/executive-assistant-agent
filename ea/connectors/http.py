"""One place that knows where each local service lives.

Running on the Mac: services are on localhost. Running on a server: they are reached through the Mac
bridge (bridge/mac_bridge.py) over Tailscale, which prefixes paths with the service name and wants its token
in `X-Bridge-Token` (so `Authorization` stays free for the tracker's own token). Connectors ask the router for
a client and never care which. Through the bridge the Mac may be asleep: that is reported in plain words."""
from __future__ import annotations

from typing import Any

import httpx

from .. import config


class SourceError(RuntimeError):
    pass


class ServiceRouter:
    def __init__(self, settings: config.Settings | None = None):
        self.s = (settings or config.load()).sources
        self.bridge = self.s.bridge_url.rstrip("/")
        self.token = config.secret(self.s.bridge_token_env)

    def client(self, service: str) -> httpx.Client:
        if self.bridge:
            if not self.token:
                raise SourceError(f"{self.s.bridge_token_env} is not set but sources.bridge_url is")
            return httpx.Client(base_url=f"{self.bridge}/{service}", timeout=self.s.timeout_seconds,
                                headers={"X-Bridge-Token": self.token})
        base = {"tracker": self.s.tracker_url, "focus": self.s.focus_url, "bandit": self.s.bandit_url}[service]
        return httpx.Client(base_url=base.rstrip("/"), timeout=self.s.timeout_seconds)

    def describe(self) -> str:
        return f"bridge {self.bridge}" if self.bridge else "direct (localhost)"

    def mac(self, method: str, path: str, body: Any = None, raw: bool = False, params: dict[str, Any] | None = None) -> Any:
        """A call to the bridge itself (not one of the three services): /mac/open-tracker, /inbox, /documents.
        `raw`: the response body as bytes instead of parsed JSON."""
        if not self.bridge or not self.token:
            raise SourceError("no Mac bridge configured (sources.bridge_url)")
        with httpx.Client(base_url=self.bridge, timeout=self.s.timeout_seconds, headers={"X-Bridge-Token": self.token}) as c:
            if not raw:
                return send_json(c, method, path, body, **(params or {}))
            try:
                r = c.request(method, path, params=params)
                r.raise_for_status()
                return r.content
            except httpx.HTTPStatusError as e:
                raise SourceError(f"{method} {path} -> {e.response.status_code}: {e.response.text[:300]}") from e
            except httpx.HTTPError as e:
                raise SourceError(_unreachable(c, e) or f"{method} {path}: {e}") from e


MAC_AWAY = "Eli's Mac is not reachable (asleep, shut, or off the network), and this lives on the Mac; it will work again when the Mac is awake"


def _unreachable(client: httpx.Client, e: Exception) -> str | None:
    """Through the bridge, a connection that cannot be made means the Mac is away, not that something is broken."""
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)) and "x-bridge-token" in client.headers:
        return MAC_AWAY
    return None


def get_json(client: httpx.Client, path: str, **params: Any) -> Any:
    try:
        r = client.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        raise SourceError(f"{e.request.url} -> {e.response.status_code}: {e.response.text[:300]}") from e
    except httpx.HTTPError as e:
        raise SourceError(_unreachable(client, e) or f"{path}: {e}") from e


def send_json(client: httpx.Client, method: str, path: str, body: Any = None, **params: Any) -> Any:
    try:
        r = client.request(method, path, json=body, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json() if r.content else None
    except httpx.HTTPStatusError as e:
        raise SourceError(f"{method} {e.request.url} -> {e.response.status_code}: {e.response.text[:300]}") from e
    except httpx.HTTPError as e:
        raise SourceError(_unreachable(client, e) or f"{method} {path}: {e}") from e
