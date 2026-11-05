"""Conversation state: a rolling history per thread in SQLite, plus append-only daily transcripts.

The transcripts are the audit trail the memory dates point at. The SQLite history is what the model sees;
`window()` trims it to the last N messages without ever splitting a tool_use from its tool_result.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, thread TEXT NOT NULL, ts REAL NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, channel TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread, id);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY, due_ts REAL NOT NULL, text TEXT NOT NULL, channel TEXT DEFAULT 'telegram',
    created_ts REAL NOT NULL, sent_ts REAL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

MAIN_THREAD = "main"


class ConversationStore:
    def __init__(self, db_path: pathlib.Path, transcripts_dir: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.transcripts_dir = pathlib.Path(transcripts_dir)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: Any = None) -> Any:
        r = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, json.dumps(value)))
            self._conn.commit()

    # ------------------------------------------------------------------ messages
    def append(self, thread: str, role: str, content: Any, channel: str = "") -> int:
        ts = time.time()
        with self._lock:
            cur = self._conn.execute("INSERT INTO messages(thread,ts,role,content,channel) VALUES(?,?,?,?,?)",
                                     (thread, ts, role, json.dumps(content, default=str), channel))
            self._conn.commit()
        self._transcribe(thread, ts, role, content, channel)
        return int(cur.lastrowid)

    def _transcribe(self, thread: str, ts: float, role: str, content: Any, channel: str) -> None:
        day = dt.datetime.fromtimestamp(ts).date().isoformat()
        rec = {"ts": dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds"), "thread": thread,
               "channel": channel, "role": role, "content": content}
        with (self.transcripts_dir / f"{day}.jsonl").open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def history(self, thread: str, limit: int = 80) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT role, content FROM messages WHERE thread=? ORDER BY id DESC LIMIT ?",
                                  (thread, limit * 2)).fetchall()
        msgs = [{"role": r["role"], "content": json.loads(r["content"])} for r in reversed(rows)]
        return window(msgs, limit)

    def new_thread(self, base: str = MAIN_THREAD) -> str:
        """Start a fresh thread (used when the agent ends a conversation). Old messages stay in the DB."""
        name = f"{base}@{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.set("current_thread", name)
        return name

    def current_thread(self) -> str:
        return self.get("current_thread", MAIN_THREAD)

    def recent_days(self, days: int = 3) -> list[str]:
        """Transcript dates available for the last `days` days (for the agent to cite)."""
        out = []
        for i in range(days):
            d = (dt.date.today() - dt.timedelta(days=i)).isoformat()
            if (self.transcripts_dir / f"{d}.jsonl").exists():
                out.append(d)
        return out

    # ------------------------------------------------------------------ reminders
    def add_reminder(self, due: dt.datetime, text: str, channel: str = "telegram") -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO reminders(due_ts,text,channel,created_ts) VALUES(?,?,?,?)",
                                     (due.timestamp(), text, channel, time.time()))
            self._conn.commit()
        return int(cur.lastrowid)

    def pending_reminders(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM reminders WHERE sent_ts IS NULL ORDER BY due_ts").fetchall()
        return [dict(r) for r in rows]

    def due_reminders(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        rows = self._conn.execute("SELECT * FROM reminders WHERE sent_ts IS NULL AND due_ts<=? ORDER BY due_ts", (now,)).fetchall()
        return [dict(r) for r in rows]

    def mark_sent(self, rid: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE reminders SET sent_ts=? WHERE id=?", (time.time(), rid))
            self._conn.commit()

    def cancel_reminder(self, rid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM reminders WHERE id=? AND sent_ts IS NULL", (rid,))
            self._conn.commit()
        return cur.rowcount > 0


def window(msgs: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Last `limit` messages, cut at a plain user message so no tool_result is left without its tool_use
    and the history starts with a user turn (the API requires it)."""
    if len(msgs) > limit:
        msgs = msgs[-limit:]
    for i, m in enumerate(msgs):
        if m["role"] == "user" and not _has_tool_result(m):
            return msgs[i:]
    return []


def _has_tool_result(m: dict[str, Any]) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
