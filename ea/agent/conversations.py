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
    created_ts REAL NOT NULL, sent_ts REAL, kind TEXT DEFAULT 'message', every_minutes INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS doc_snapshots (name TEXT PRIMARY KEY, mtime REAL NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, engine TEXT, billing TEXT, model TEXT, speed TEXT NOT NULL,
    mode TEXT, channel TEXT, thread TEXT, input INTEGER, cache_read INTEGER, cache_write_5m INTEGER,
    cache_write_1h INTEGER, output INTEGER, cost_usd REAL
);
CREATE INDEX IF NOT EXISTS llm_usage_ts ON llm_usage(ts);
"""

MAIN_THREAD = "main"
MID_TURN_PREFIX = "[The user sent this while you were working; take it into account:]\n"


class _Rows(list):
    """Rows already fetched under the lock, with the two cursor methods the callers use."""
    def fetchall(self) -> "_Rows":
        return self

    def fetchone(self) -> Any:
        return self[0] if self else None


class ConversationStore:
    def __init__(self, db_path: pathlib.Path, transcripts_dir: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        self.transcripts_dir = pathlib.Path(transcripts_dir)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        for col, ddl in (("kind", "TEXT DEFAULT 'message'"), ("every_minutes", "INTEGER")):   # databases from before wakeups
            if col not in {r[1] for r in self._conn.execute("PRAGMA table_info(reminders)")}:
                self._conn.execute(f"ALTER TABLE reminders ADD COLUMN {col} {ddl}")
        self._conn.commit()

    def _q(self, sql: str, args: Any = ()) -> "_Rows":
        """Every read goes through here. The connection is shared by the turn threads, the scheduler, the keep-alive
        loop, workers and the web server; a read that overlapped a write either raised "bad parameter or other API
        misuse" or came back empty, and `get` then answered its default as if the key were not there (a check run
        twice, a session re-forked). Found 2026-11-20; the workspace store had the same fault on 11-12."""
        with self._lock:
            return _Rows(self._conn.execute(sql, args).fetchall())

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: Any = None) -> Any:
        r = self._q("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, json.dumps(value)))
            self._conn.commit()

    # ------------------------------------------------------------------ messages
    # ------------------------------------------------------------------ model usage (see ea/usage.py)
    def record_usage(self, row: dict[str, Any]) -> None:
        cols = ("ts", "engine", "billing", "model", "speed", "mode", "channel", "thread", "input", "cache_read",
                "cache_write_5m", "cache_write_1h", "output", "cost_usd")
        with self._lock:
            self._conn.execute(f"INSERT INTO llm_usage({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [row.get(c) for c in cols])
            self._conn.commit()

    def usage_totals(self, since_ts: float = 0.0) -> list[dict[str, Any]]:
        """Per (speed, billing) since a time; `unpriced` counts calls on a model with no known price."""
        rows = self._q(
            "SELECT speed, billing, COUNT(*) AS calls, SUM(input) AS input, SUM(cache_read) AS cache_read, "
            "SUM(cache_write_5m + cache_write_1h) AS cache_write, SUM(output) AS output, SUM(cost_usd) AS cost_usd, "
            "SUM(cost_usd IS NULL) AS unpriced FROM llm_usage WHERE ts >= ? GROUP BY speed, billing ORDER BY speed, billing", (since_ts,))
        return [dict(r) for r in rows]

    def usage_rows(self, since_ts: float = 0.0) -> list[dict[str, Any]]:
        """Every recorded model call since a time, oldest first (for the per-day breakdown)."""
        return [dict(r) for r in self._q("SELECT * FROM llm_usage WHERE ts >= ? ORDER BY id", (since_ts,))]

    def usage_first_ts(self) -> float | None:
        return self._q("SELECT MIN(ts) FROM llm_usage").fetchone()[0]

    def append(self, thread: str, role: str, content: Any, channel: str = "") -> int:
        ts = time.time()
        with self._lock:
            cur = self._conn.execute("INSERT INTO messages(thread,ts,role,content,channel) VALUES(?,?,?,?,?)",
                                     (thread, ts, role, json.dumps(content, default=str), channel))
            self._conn.commit()
        self._transcribe(thread, ts, role, content, channel)
        return int(cur.lastrowid)

    def last_id(self, thread: str) -> int:
        return int(self._q("SELECT COALESCE(MAX(id), 0) FROM messages WHERE thread=?", (thread,)).fetchone()[0])

    def has_rows_before(self, thread: str, before_id: int | None = None) -> bool:
        """Whether the thread holds anything older than message `before_id` (or anything at all)."""
        q, args = "SELECT 1 FROM messages WHERE thread=?", [thread]
        if before_id is not None:
            q += " AND id<?"; args.append(before_id)
        return bool(self._q(q + " LIMIT 1", args))

    def rows_between(self, thread: str, after_id: int, before_id: int | None = None) -> list[dict[str, Any]]:
        """Stored messages of a thread with after_id < id < before_id, oldest first (for agent/catchup.py)."""
        q, args = "SELECT id, ts, role, channel, content FROM messages WHERE thread=? AND id>?", [thread, after_id]
        if before_id is not None:
            q += " AND id<?"; args.append(before_id)
        return [{**dict(r), "content": json.loads(r["content"])} for r in self._q(q + " ORDER BY id", args)]

    def delete(self, msg_id: int) -> None:
        """Take back a message that never got an answer (a turn whose model call failed), so it does not sit in
        the history as a dangling instruction for the next turn. The transcript keeps it."""
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE id=?", (msg_id,))
            self._conn.commit()

    def _transcribe(self, thread: str, ts: float, role: str, content: Any, channel: str) -> None:
        day = dt.datetime.fromtimestamp(ts).date().isoformat()
        rec = {"ts": dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds"), "thread": thread,
               "channel": channel, "role": role, "content": content}
        with (self.transcripts_dir / f"{day}.jsonl").open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def history(self, thread: str, limit: int = 80) -> list[dict[str, Any]]:
        rows = self._q("SELECT role, content FROM messages WHERE thread=? ORDER BY id DESC LIMIT ?",
                                  (thread, limit * 2)).fetchall()
        msgs = [{"role": r["role"], "content": json.loads(r["content"])} for r in reversed(rows)]
        return repair(window(msgs, limit))

    def readable(self, limit: int = 60, all_threads: bool = True) -> list[dict[str, Any]]:
        """The conversation as a person would read it, newest last: what the user said (not the scheduler's check
        prompts, not tool results) and what the agent replied or sent (not its tool calls). For the chat page."""
        # Only rows that can show as a chat line: text somewhere in the content, and not the scheduler's own prompts.
        # (Filtering in SQL matters: a busy agent writes hundreds of tool rows an hour, which used to push the real
        # chat out of a fixed-size window and empty the app's thread - seen 2026-11-06.)
        # The chat shows what passed between the user and the agent: the user's messages, the agent's replies, and
        # what it sent on its own (channel 'proactive'). The closing notes of scheduled / worker-result turns
        # (channel 'scheduler' or 'worker') are log entries, not messages, and live in the thinking pane only.
        where = ("WHERE (content LIKE '%\"type\": \"text\"%' OR content LIKE '%\"type\": \"image\"%') "
                 "AND channel NOT IN ('scheduler', 'worker') AND thread NOT LIKE 'worker:%'")
        q = f"SELECT id, ts, role, channel, content FROM messages {where}" + ("" if all_threads else " AND thread=?") + " ORDER BY id DESC LIMIT ?"
        args = (limit * 2,) if all_threads else (self.current_thread(), limit * 2)
        out: list[dict[str, Any]] = []
        for r in self._q(q, args).fetchall():
            if r["role"] == "user" and r["channel"] == "scheduler":
                continue
            content = json.loads(r["content"])
            images: list[str] = []
            if isinstance(content, str):
                text = content
            else:
                text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                images = [b["source"]["name"] for b in content if isinstance(b, dict) and b.get("type") == "image"
                          and isinstance(b.get("source"), dict) and b["source"].get("type") == "file"]
            text = text.strip()
            if not text and not images:
                continue
            row = {"id": r["id"], "ts": dt.datetime.fromtimestamp(r["ts"]).isoformat(timespec="minutes"),
                   "who": "me" if r["role"] == "user" else "it", "channel": r["channel"],
                   "text": text.removeprefix("[sent via send_message] ").removeprefix(MID_TURN_PREFIX)}
            if images:
                row["images"] = images
            out.append(row)
            if len(out) >= limit:
                break
        return list(reversed(out))

    def activity(self, limit: int = 300) -> list[dict[str, Any]]:
        """The agent's chain of thought as stored: for each turn, what started it, its thinking, intermediate text,
        tool calls with inputs, tool results, and the final reply. Same event shapes as the live feed emits.
        Newest `limit` events, oldest first."""
        out: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        q = "SELECT id, ts, role, channel, content FROM messages ORDER BY id DESC LIMIT ?"
        rows = self._q(q, (limit * 2,)).fetchall()
        for r in reversed(rows):
            ts = dt.datetime.fromtimestamp(r["ts"]).isoformat(timespec="seconds")
            content = json.loads(r["content"])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if r["role"] == "user":
                results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                if results:
                    for b in results:
                        out.append({"kind": "tool_result", "id": b.get("tool_use_id"), "name": names.get(b.get("tool_use_id"), "?"),
                                    "output": _clip(b.get("content"), 1500), "error": bool(b.get("is_error")), "ts": ts})
                    continue
                text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
                out.append({"kind": "turn", "channel": r["channel"], "label": text[:160], "ts": ts, "msg": r["id"]})
            else:
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "thinking" and b.get("thinking"):
                        out.append({"kind": "thinking", "text": b["thinking"], "ts": ts})
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        out.append({"kind": "text", "text": b["text"], "ts": ts,
                                    "channel": r["channel"], "final": not any(x.get("type") == "tool_use" for x in content if isinstance(x, dict))})
                    elif b.get("type") == "tool_use":
                        names[b.get("id", "")] = b.get("name", "?")
                        out.append({"kind": "tool_call", "id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}, "ts": ts})
        return out[-limit:]

    # ------------------------------------------------------------------ document snapshots (for change notices)
    def doc_snapshot(self, name: str) -> tuple[float, str] | None:
        r = self._q("SELECT mtime, text FROM doc_snapshots WHERE name=?", (name,)).fetchone()
        return (r["mtime"], r["text"]) if r else None

    def set_doc_snapshot(self, name: str, mtime: float, text: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO doc_snapshots(name, mtime, text) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET mtime=excluded.mtime, text=excluded.text",
                               (name, mtime, text))
            self._conn.commit()

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
    def add_reminder(self, due: dt.datetime, text: str, channel: str = "telegram", kind: str = "message",
                     every_minutes: int | None = None) -> int:
        """kind 'message': `text` is sent to the user at `due`. kind 'wakeup': `text` is a prompt the agent runs
        a proactive turn on at `due`. `every_minutes` repeats either one until cancelled."""
        with self._lock:
            cur = self._conn.execute("INSERT INTO reminders(due_ts,text,channel,created_ts,kind,every_minutes) VALUES(?,?,?,?,?,?)",
                                     (due.timestamp(), text, channel, time.time(), kind, every_minutes))
            self._conn.commit()
        return int(cur.lastrowid)

    def pending_reminders(self, kind: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM reminders WHERE sent_ts IS NULL", []
        if kind:
            q += " AND kind=?"; args.append(kind)
        rows = self._q(q + " ORDER BY due_ts", args).fetchall()
        return [dict(r) for r in rows]

    def due_reminders(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        rows = self._q("SELECT * FROM reminders WHERE sent_ts IS NULL AND due_ts<=? ORDER BY due_ts", (now,)).fetchall()
        return [dict(r) for r in rows]

    def mark_sent(self, rid: int, now: float | None = None) -> None:
        """Done with this firing: a one-off is marked sent; a repeating one moves to its next future due time."""
        now = now or time.time()
        with self._lock:
            r = self._conn.execute("SELECT due_ts, every_minutes FROM reminders WHERE id=?", (rid,)).fetchone()
            if r and r["every_minutes"]:
                step = r["every_minutes"] * 60
                nxt = r["due_ts"] + step
                if nxt <= now:                                   # several periods missed (machine asleep): skip them
                    nxt += step * (int((now - nxt) // step) + 1)
                self._conn.execute("UPDATE reminders SET due_ts=? WHERE id=?", (nxt, rid))
            else:
                self._conn.execute("UPDATE reminders SET sent_ts=? WHERE id=?", (now, rid))
            self._conn.commit()

    def cancel_reminder(self, rid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM reminders WHERE id=? AND sent_ts IS NULL", (rid,))
            self._conn.commit()
        return cur.rowcount > 0


def _clip(x: Any, n: int) -> str:
    if isinstance(x, list):
        x = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in x)
    s = str(x) if x is not None else ""
    return s if len(s) <= n else s[:n] + f"… [{len(s) - n} more chars]"


def window(msgs: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Last `limit` messages, cut at a plain user message so no tool_result is left without its tool_use
    and the history starts with a user turn (the API requires it)."""
    if len(msgs) > limit:
        msgs = msgs[-limit:]
    for i, m in enumerate(msgs):
        if m["role"] == "user" and not _has_tool_result(m):
            return msgs[i:]
    return []


INTERRUPTED = "(interrupted: this tool call has no recorded result)"


def repair(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make stored history acceptable to the API again: every tool_use is answered right after the assistant's turn,
    and no tool_result is left without its call. Stored rows break that in ways the API then refuses with a 400,
    and since a refused turn adds no rows the thread stayed refused (found by reading, 2026-11-20):
    a restart or a crash while a tool ran (call stored, result never); a message the user typed while a tool was
    running, stored between the call and its result; a response cut off at max_tokens with a half-written call.
    Only the copy handed to the model is changed; the stored rows stay as they happened."""
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(msgs):
        if msgs[i]["role"] != "assistant":
            kept = _without_results(msgs[i])               # a result with no call before it in this window
            if kept is not None:
                out.append(kept)
            i += 1
            continue
        calls: list[str] = []
        while i < len(msgs) and msgs[i]["role"] == "assistant":
            c = msgs[i].get("content")
            calls += [b["id"] for b in (c if isinstance(c, list) else []) if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")]
            out.append(msgs[i]); i += 1
        users = []
        while i < len(msgs) and msgs[i]["role"] == "user":
            users.append(msgs[i]); i += 1
        if not calls:
            for u in users:
                kept = _without_results(u)
                if kept is not None:
                    out.append(kept)
            continue
        got: dict[str, dict[str, Any]] = {}
        for u in users:
            for b in (u["content"] if isinstance(u.get("content"), list) else []):
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    got.setdefault(b["tool_use_id"], b)
        out.append({"role": "user", "content": [got.get(c) or {"type": "tool_result", "tool_use_id": c, "content": INTERRUPTED, "is_error": True}
                                                for c in dict.fromkeys(calls)]})
        for u in users:                                    # whatever else the user rows held (typed text, images) follows the results
            kept = _without_results(u)
            if kept is not None:
                out.append(kept)
    return out


def _without_results(m: dict[str, Any]) -> dict[str, Any] | None:
    c = m.get("content")
    if not isinstance(c, list):
        return m if c else None
    rest = [b for b in c if not (isinstance(b, dict) and b.get("type") == "tool_result")]
    if len(rest) == len(c):
        return m
    return {**m, "content": rest} if rest else None


def _has_tool_result(m: dict[str, Any]) -> bool:
    c = m.get("content")
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
