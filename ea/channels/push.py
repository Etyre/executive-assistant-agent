"""Apple Push Notification service: the text channel to the dedicated iPhone and Mac apps.

The apps register their device tokens at `POST /api/devices`; everything the agent sends proactively
(send_message in a check, a reminder, a heartbeat note) is pushed to every registered device. Token-based
APNs auth (a .p8 key from the developer portal, signed as an ES256 JWT, cached for 50 minutes) over HTTP/2.
Needs: APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID in .env and channels.apns_bundle_id in config.toml.

Sandbox vs production is per device: a debug build from Xcode gets sandbox tokens and says so when it
registers, a TestFlight/App Store build gets production ones."""
from __future__ import annotations

import base64
import json
import logging
import pathlib
import sqlite3
import threading
import time
from typing import Any

import httpx

from .. import config

log = logging.getLogger(__name__)

HOSTS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
JWT_TTL = 50 * 60                 # Apple: refresh at least hourly, not more than every 20 min
MAX_BODY = 3000                   # APNs payload limit is 4 KB; the app fetches the full text from /api/history
UNREGISTER_REASONS = {"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    token TEXT PRIMARY KEY, platform TEXT NOT NULL, name TEXT DEFAULT '', env TEXT NOT NULL DEFAULT 'production',
    added_ts REAL NOT NULL, seen_ts REAL NOT NULL
);
"""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(private_key_pem: bytes, key_id: str, team_id: str, now: float | None = None) -> str:
    """ES256 provider token, as APNs wants it: header {alg, kid}, claims {iss, iat}."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    header = _b64(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"iss": team_id, "iat": int(now or time.time())}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    return f"{header}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class Devices:
    """Registered app installs, in the same SQLite file as the conversation state."""

    def __init__(self, db_path: pathlib.Path):
        self._conn = sqlite3.connect(pathlib.Path(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def register(self, token: str, platform: str, name: str = "", env: str = "production") -> dict[str, Any]:
        token = token.strip().lower()
        if not token or len(token) < 32 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("device token must be the hex string the OS gives the app")
        if env not in HOSTS:
            raise ValueError("env must be 'production' or 'sandbox'")
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices(token,platform,name,env,added_ts,seen_ts) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(token) DO UPDATE SET platform=excluded.platform, name=excluded.name, env=excluded.env, seen_ts=excluded.seen_ts",
                (token, platform, name, env, now, now))
            self._conn.commit()
        return self.get(token)

    def get(self, token: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM devices WHERE token=?", (token.strip().lower(),)).fetchone()
        return dict(r) if r else None

    def remove(self, token: str) -> bool:
        with self._lock:
            n = self._conn.execute("DELETE FROM devices WHERE token=?", (token.strip().lower(),)).rowcount
            self._conn.commit()
        return n > 0

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM devices ORDER BY added_ts").fetchall()]


class APNs:
    """One provider connection per environment; JWT cached and refreshed."""

    def __init__(self, key_pem: bytes, key_id: str, team_id: str, bundle_id: str, transport: httpx.BaseTransport | None = None):
        self.key_pem, self.key_id, self.team_id, self.bundle_id = key_pem, key_id, team_id, bundle_id
        self._jwt: str | None = None
        self._jwt_ts = 0.0
        self._lock = threading.Lock()
        kw: dict[str, Any] = {"timeout": 15}
        if transport is not None:
            kw["transport"] = transport
        else:
            kw["http2"] = True
        self._clients = {env: httpx.Client(base_url=host, **kw) for env, host in HOSTS.items()}

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._jwt or time.time() - self._jwt_ts > JWT_TTL:
                self._jwt = make_jwt(self.key_pem, self.key_id, self.team_id)
                self._jwt_ts = time.time()
            return self._jwt

    def send(self, device_token: str, payload: dict[str, Any], env: str = "production", collapse_id: str | None = None,
             push_type: str = "alert", priority: int = 10) -> tuple[int, str]:
        """Returns (status, reason). 200 means delivered to Apple. Retries once on an expired provider token."""
        headers = {"apns-topic": self.bundle_id, "apns-push-type": push_type, "apns-priority": str(priority), "apns-expiration": "0"}
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        body = json.dumps(payload, ensure_ascii=False).encode()
        for attempt in (0, 1):
            headers["authorization"] = f"bearer {self.token(force=attempt == 1)}"
            try:
                r = self._clients[env].post(f"/3/device/{device_token}", content=body, headers=headers)
            except httpx.HTTPError as e:
                return 0, f"{type(e).__name__}: {e}"
            if r.status_code == 200:
                return 200, "ok"
            try:
                reason = r.json().get("reason", r.text[:100])
            except ValueError:
                reason = r.text[:100]
            if r.status_code == 403 and reason in ("ExpiredProviderToken", "InvalidProviderToken") and attempt == 0:
                continue
            return r.status_code, reason
        return 0, "unreachable"


class Push:
    """The channel object the serving process wires in: `send_sync(text)` from the agent's thread."""

    def __init__(self, settings: config.Settings | None = None, apns: APNs | None = None, devices: Devices | None = None):
        self.s = settings or config.load()
        ch = self.s.channels
        self.devices = devices or Devices(self.s.state_db)
        self.apns = apns
        self.reason_disabled = ""
        if self.apns is None:
            key_path = config.secret(ch.apns_key_path_env)
            key_id, team_id = config.secret(ch.apns_key_id_env), config.secret(ch.apns_team_id_env)
            missing = [n for n, v in ((ch.apns_key_path_env, key_path), (ch.apns_key_id_env, key_id), (ch.apns_team_id_env, team_id)) if not v]
            if missing:
                self.reason_disabled = "not set: " + ", ".join(missing)
            elif not ch.apns_bundle_id:
                self.reason_disabled = "channels.apns_bundle_id is empty"
            else:
                p = pathlib.Path(key_path).expanduser()
                if not p.is_absolute():
                    p = self.s.data_dir / p
                if not p.exists():
                    self.reason_disabled = f"key file {p} not found"
                else:
                    self.apns = APNs(p.read_bytes(), key_id, team_id, ch.apns_bundle_id)

    def enabled(self) -> bool:
        return self.apns is not None

    def describe(self) -> str:
        n = len(self.devices.all())
        return f"configured ({self.s.channels.apns_bundle_id}); {n} device(s) registered" if self.enabled() else f"not configured ({self.reason_disabled}); {n} device(s) registered"

    def payload(self, text: str, title: str | None = None, kind: str = "message") -> dict[str, Any]:
        body = text if len(text) <= MAX_BODY else text[:MAX_BODY - 1] + "…"
        aps: dict[str, Any] = {"alert": {"title": title or self.s.agent_name, "body": body}, "sound": "default", "thread-id": "chat"}
        if self.s.channels.apns_interruption_level:
            aps["interruption-level"] = self.s.channels.apns_interruption_level
        return {"aps": aps, "kind": kind, "ts": time.time()}

    def send(self, text: str, only: list[str] | None = None, exclude: list[str] | None = None, title: str | None = None,
             kind: str = "message", collapse_id: str | None = None) -> list[dict[str, Any]]:
        """Push `text` to every registered device (or `only` these tokens, minus `exclude`). Tokens Apple reports
        as gone are forgotten. Returns one result per device."""
        results = []
        targets = self.devices.all()
        if only is not None:
            want = {t.strip().lower() for t in only}
            targets = [d for d in targets if d["token"] in want]
        if exclude:
            skip = {t.strip().lower() for t in exclude}
            targets = [d for d in targets if d["token"] not in skip]
        if not targets:
            return results
        if not self.enabled():
            log.warning("push not configured (%s); %d device(s) not notified: %s", self.reason_disabled, len(targets), text[:80])
            return [{"token": d["token"], "status": 0, "reason": "apns not configured"} for d in targets]
        payload = self.payload(text, title, kind)
        for d in targets:
            status, reason = self.apns.send(d["token"], payload, env=d["env"], collapse_id=collapse_id)
            if status != 200:
                log.warning("push to %s (%s/%s) failed %s %s", d["name"] or d["token"][:8], d["platform"], d["env"], status, reason)
                if reason in UNREGISTER_REASONS:
                    self.devices.remove(d["token"])
                    reason += " (device forgotten)"
            results.append({"token": d["token"], "platform": d["platform"], "name": d["name"], "status": status, "reason": reason})
        return results

    def send_sync(self, text: str) -> None:
        """Outbound hook for the agent (send_message tool, reminders): fan out to every device."""
        self.send(text)
