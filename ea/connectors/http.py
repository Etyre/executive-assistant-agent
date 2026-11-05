"""One place that knows where each local service lives.

Running on the Mac: services are on localhost. Running in the cloud: they are reached through the Mac
bridge (bridge/mac_bridge.py) over Tailscale, which prefixes paths with the service name and wants a
bearer token. Connectors ask the router for a client and never care which."""
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
                                headers={"Authorization": f"Bearer {self.token}"})
        base = {"tracker": self.s.tracker_url, "focus": self.s.focus_url, "bandit": self.s.bandit_url}[service]
        return httpx.Client(base_url=base.rstrip("/"), timeout=self.s.timeout_seconds)

    def describe(self) -> str:
        return f"bridge {self.bridge}" if self.bridge else "direct (localhost)"


def get_json(client: httpx.Client, path: str, **params: Any) -> Any:
    try:
        r = client.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        raise SourceError(f"{e.request.url} -> {e.response.status_code}: {e.response.text[:300]}") from e
    except httpx.HTTPError as e:
        raise SourceError(f"{path}: {e}") from e


def send_json(client: httpx.Client, method: str, path: str, body: Any = None, **params: Any) -> Any:
    try:
        r = client.request(method, path, json=body, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json() if r.content else None
    except httpx.HTTPStatusError as e:
        raise SourceError(f"{method} {e.request.url} -> {e.response.status_code}: {e.response.text[:300]}") from e
    except httpx.HTTPError as e:
        raise SourceError(f"{method} {path}: {e}") from e
