"""In-process event bus: whatever the agent sends or says is published here, and every open app (iPhone, Mac,
web page) that holds `GET /api/events` open receives it live. Publishing is thread-safe (the agent runs in
worker threads); subscribers are asyncio queues owned by the event loop that serves the streams."""
from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = threading.Lock()
        self._next = 1

    def subscribe(self) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        """Call from inside the event loop that will read the queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        with self._lock:
            sid = self._next
            self._next += 1
            self._subs[sid] = (asyncio.get_running_loop(), q)
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def publish(self, event: dict[str, Any]) -> int:
        """From any thread. Returns the number of subscribers it was handed to."""
        event = {**event}
        event.setdefault("ts", dt.datetime.now().isoformat(timespec="seconds"))
        with self._lock:
            subs = list(self._subs.values())
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_put, q, event)
            except RuntimeError:          # loop closed
                pass
        return len(subs)

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)


def _put(q: asyncio.Queue, event: dict[str, Any]) -> None:
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:         # a stalled client; drop rather than block the agent
        pass


class Presence:
    """Is the user in the chat right now? Two signals: open apps/pages report that their chat is in front
    (`POST /api/presence`, refreshed every 30 s, expiring after `active_ttl`), and a message the user just typed
    counts for `typed_ttl` seconds. While in the chat, the agent's messages are marked quiet: they appear in the
    thread, but no client raises a notification and no phone push is sent."""

    def __init__(self, active_ttl: float = 75.0, typed_ttl: float = 180.0):
        self.active_ttl, self.typed_ttl = active_ttl, typed_ttl
        self._active: dict[str, float] = {}
        self._last_typed = 0.0
        self._lock = threading.Lock()

    def report(self, client_id: str, active: bool, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            if active:
                self._active[client_id] = now
            else:
                self._active.pop(client_id, None)

    def typed(self, now: float | None = None) -> None:
        with self._lock:
            self._last_typed = now or time.time()

    def in_chat(self, now: float | None = None, typed: bool = True) -> bool:
        """`typed=False`: only count clients whose chat is in front (for the reply push to a phone that may have
        locked since the user typed)."""
        now = now or time.time()
        with self._lock:
            self._active = {k: t for k, t in self._active.items() if now - t < self.active_ttl}
            return bool(self._active) or (typed and now - self._last_typed < self.typed_ttl)

    def describe(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        with self._lock:
            active = sorted(k for k, t in self._active.items() if now - t < self.active_ttl)
        return {"in_chat": self.in_chat(now), "active_clients": active,
                "seconds_since_typed": None if not self._last_typed else int(now - self._last_typed)}


BUS = EventBus()
PRESENCE = Presence()
