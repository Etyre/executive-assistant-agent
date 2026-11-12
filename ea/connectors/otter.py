"""Otter.ai transcripts, read-only, from two places merged into one local index:

1. **Otter itself**, through the same unofficial cookie API the open-source clients and
   `logseq-and-roam/otter-import/logseq/otter2logseq.py` use (login with email + password, then
   `/speeches` and `/speech?otid=`). Credentials: `OTTER_EMAIL` / `OTTER_PASSWORD` (an account that signs in
   through Google needs an Otter password set on otter.ai first). Nothing is ever written to Otter.
2. **The Logseq graph**, where the Roam extension auto-imported every recording from January 2024 on, in
   three pages ("Otter auto-import walking journal transcripts", "... audio notes transcripts",
   "... transcripts (no folder)"). That archive is parsed straight from the page files, so the history is
   available even before Otter credentials exist, and even if Otter's API changes.

Everything lands in `<EA_DATA>/otter.db` (one row per recording keyed by Otter id, plus an FTS5 index), so
a recording that is in both places is stored once; the live copy wins because it carries the title, folder
and duration. Sync is lazy: the first read in a process after `sync_minutes` re-pulls the newest recordings
from Otter; a failed login is remembered for the same period so one outage does not slow every turn."""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
import re
import sqlite3
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .. import config
from .http import SourceError

log = logging.getLogger(__name__)

API = "https://otter.ai/forward/api/v1"
ARCHIVE_PAGES = {                       # Logseq page title -> folder name used in the index
    "Otter auto-import walking journal transcripts": "walking journal",
    "Otter auto-imported audio notes transcripts": "audio notes",
    "Otter auto-imported transcripts (no folder)": "",
}
_ORDINAL = re.compile(r"(\d+)(st|nd|rd|th)")
_PROP = re.compile(r"^\s*([A-Za-z][\w-]*)::\s*(.*)$")
_LINK = re.compile(r"\[\[([^\]]*)\]\]")
_BLOCK_ID = re.compile(r"^\s*id::\s*[0-9a-f-]{36}\s*$")
_LABEL_TITLE = re.compile(r"\]\]\s*(?:#\[\[[^\]]*\]\]\s*)*\((.+)\)\s*$")


# ----------------------------------------------------------------------------- Logseq archive
class _Block:
    __slots__ = ("depth", "lines", "children", "parent")

    def __init__(self, depth: int, first: str, parent: "_Block | None"):
        self.depth, self.lines, self.children, self.parent = depth, [first], [], parent

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()


def parse_logseq_page(text: str) -> list[_Block]:
    """Logseq's file layout: `<tabs>- text` opens a block, `<tabs>  text` continues it; `id::` lines dropped."""
    roots: list[_Block] = []
    stack: list[_Block] = []
    for raw in text.splitlines():
        m = re.match(r"^(\t*)- (.*)$", raw)
        if m:
            depth = len(m.group(1))
            while stack and stack[-1].depth >= depth:
                stack.pop()
            b = _Block(depth, m.group(2), stack[-1] if stack else None)
            (stack[-1].children if stack else roots).append(b)
            stack.append(b)
        elif stack:
            if _BLOCK_ID.match(raw):
                continue
            stack[-1].lines.append(raw.strip("\t").removeprefix("  ") if raw.strip() else "")
    return roots


def _roam_date(s: str) -> str | None:
    m = _LINK.search(s)
    cand = m.group(1) if m else s
    try:
        return dt.datetime.strptime(_ORDINAL.sub(r"\1", cand.strip()), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def parse_archive_page(text: str, folder: str) -> list[dict[str, Any]]:
    """Each recording the Roam extension wrote: metadata block (Date, start-time, otter-link, Source) under a
    label block, with the transcript block beside it."""
    out: list[dict[str, Any]] = []
    for root in parse_logseq_page(text):
        for meta in [root, *root.walk()]:
            props = {}
            for c in meta.children:
                pm = _PROP.match(c.lines[0])
                if pm:
                    props[pm.group(1).lower()] = pm.group(2).strip()
            if "otter-link" not in props or not meta.parent:
                continue
            label = meta.parent
            transcript = next((s for s in label.children if s is not meta and "transcri" in s.lines[0].lower()), None)
            parts = [d.text for d in (transcript.walk() if transcript else []) if d.text]
            link = props["otter-link"]
            otid = link.rstrip("/").rsplit("/", 1)[-1]
            date = _roam_date(props.get("date", "")) or (_roam_date(label.parent.lines[0]) if label.parent else None)
            tm = _LABEL_TITLE.search(label.lines[0])
            out.append({"otid": otid, "date": date or "", "start_time": props.get("start-time", ""),
                        "title": tm.group(1).strip() if tm else "", "folder": folder, "link": link,
                        "text": "\n\n".join(parts).strip(), "duration_s": None, "source": "logseq"})
    return out


# ----------------------------------------------------------------------------- Otter API
class OtterClient:
    """The unofficial API, as otter2logseq.py and the open-source clients use it."""

    def __init__(self, email: str, password: str, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self.email, self.password = email, password
        self.http = httpx.Client(base_url=API, timeout=timeout, transport=transport,
                                 headers={"User-Agent": "Mozilla/5.0 (Macintosh) executive-assistant-agent/0.1"})
        self.userid: str | None = None

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            r = self.http.get(path, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise SourceError(f"Otter {path} -> {e.response.status_code}: {e.response.text[:200]}") from e
        except httpx.HTTPError as e:
            raise SourceError(f"Otter {path}: {e}") from e
        except ValueError as e:
            raise SourceError(f"Otter {path} did not return JSON") from e

    def login(self) -> None:
        self.http.auth = (self.email, self.password)
        data = self._get("/login", username=self.email)
        self.http.auth = None
        self.userid = str(data.get("userid") or (data.get("user") or {}).get("id") or "")
        if not self.userid:
            raise SourceError(f"Otter login gave no user id: {str(data)[:200]}")

    def speeches(self, page_size: int = 45, max_pages: int = 1) -> list[dict[str, Any]]:
        """Newest first. Pagination follows the web client's cursors until end_of_list or max_pages."""
        if not self.userid:
            self.login()
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"userid": self.userid, "folder": 0, "page_size": page_size, "source": "owned"}
        seen: set[str] = set()
        for _ in range(max(1, max_pages)):
            data = self._get("/speeches", **params)
            batch = [s for s in data.get("speeches", []) if s.get("otid") and s["otid"] not in seen]
            out.extend(batch)
            seen.update(s["otid"] for s in batch)
            if data.get("end_of_list") or not batch or not data.get("last_load_ts"):
                break
            params["last_load_ts"] = data["last_load_ts"]
            params["last_modified_at"] = data.get("last_modified_at")
            params["modified_after"] = data.get("last_modified_at") or data["last_load_ts"]   # the API rejects page 2 without it (seen 2026-11-06)
        return out

    def speech(self, otid: str) -> dict[str, Any]:
        if not self.userid:
            self.login()
        return self._get("/speech", otid=otid, userid=self.userid).get("speech", {}) or {}


def _offset_seconds(v: Any) -> float:
    v = float(v or 0)
    return v / 1000.0 if v > 100_000 else v      # some recordings report ms, others s (as otter2logseq found)


def transcript_text(speech: dict[str, Any]) -> str:
    """`[h:mm:ss] Speaker: text` paragraphs; consecutive lines by one speaker are merged."""
    paras: list[str] = []
    last_speaker: str | None = None
    for t in speech.get("transcripts", []):
        text = (t.get("transcript") or "").strip()
        if not text:
            continue
        sp = t.get("speaker_model_label") or (t.get("speaker") or {}).get("speaker_name") or "Unknown"
        start = _offset_seconds(t.get("start_offset"))
        stamp = time.strftime("%H:%M:%S", time.gmtime(start))
        if sp == last_speaker and paras:
            paras[-1] += " " + text
        else:
            paras.append(f"[{stamp}] {sp}: {text}")
            last_speaker = sp
    return "\n\n".join(paras)


def shape_speech(summary: dict[str, Any], full: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    s = {**summary, **full}
    created = dt.datetime.fromtimestamp(float(s.get("created_at") or s.get("start_time") or time.time()), tz)
    otid = s["otid"]
    title = (s.get("title") or "").strip()
    return {"otid": otid, "date": created.date().isoformat(), "start_time": created.strftime("%H:%M"),
            "title": "" if title.lower() == "untitled" else title, "folder": ((s.get("folder") or {}).get("name") or "").strip(),
            "link": f"https://otter.ai/u/{otid}", "text": transcript_text(s),
            "duration_s": int(s["duration"]) if s.get("duration") else None, "source": "otter",
            "summary": (s.get("summary") or "").strip() or None}


# ----------------------------------------------------------------------------- the index
class Otter:
    def __init__(self, settings: config.Settings | None = None, db_path: pathlib.Path | None = None,
                 client: OtterClient | None = None, now: Any = time.time):
        self.settings = s = settings or config.load()
        self.email = config.secret(s.sources.otter_email_env)
        self.password = config.secret(s.sources.otter_password_env)
        self.graph_dir = pathlib.Path(s.sources.otter_logseq_graph).expanduser() if s.sources.otter_logseq_graph else None
        self.sync_seconds = max(60, int(s.sources.otter_sync_minutes * 60))
        self.tz = ZoneInfo(s.timezone)
        self._client = client
        self._now = now
        self._lock = threading.RLock()
        self.db_path = db_path or s.data_dir / "otter.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS transcripts (
                otid TEXT PRIMARY KEY, date TEXT, start_time TEXT, title TEXT, folder TEXT, link TEXT,
                text TEXT, duration_s INTEGER, source TEXT, summary TEXT, imported_at TEXT);
            CREATE INDEX IF NOT EXISTS transcripts_date ON transcripts(date);
            CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(otid UNINDEXED, title, text);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        if "seen_ts" not in {r[1] for r in self._conn.execute("PRAGMA table_info(transcripts)")}:   # indexes from before ingestion
            self._conn.execute("ALTER TABLE transcripts ADD COLUMN seen_ts REAL")
            self._conn.commit()
        self._last_sync_attempt = 0.0
        self._last_sync_error: str | None = None

    # ----- status
    def live_enabled(self) -> bool:
        return bool(self.email and self.password)

    def archive_enabled(self) -> bool:
        return bool(self.graph_dir and (self.graph_dir / "pages").is_dir())

    def enabled(self) -> bool:
        return self.live_enabled() or self.archive_enabled() or self.count() > 0

    def count(self) -> int:
        return self._conn.execute("SELECT count(*) FROM transcripts").fetchone()[0]

    def status(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT min(date), max(date), count(*), sum(source='otter') FROM transcripts").fetchone()
        return {"recordings": row[2], "first_date": row[0], "last_date": row[1], "from_otter_live": row[3] or 0,
                "otter_login": "configured" if self.live_enabled() else "not configured (OTTER_EMAIL/OTTER_PASSWORD)",
                "logseq_archive": str(self.graph_dir) if self.archive_enabled() else "not found",
                "last_sync": self._meta("last_sync"), "last_sync_error": self._last_sync_error}

    def _meta(self, key: str) -> str | None:
        r = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # ----- writing rows
    def _upsert(self, rec: dict[str, Any], replace_live: bool = True) -> bool:
        """Insert or update one recording. A Logseq row never overwrites a live Otter row."""
        cur = self._conn.execute("SELECT source, seen_ts FROM transcripts WHERE otid=?", (rec["otid"],)).fetchone()
        if cur and cur["source"] == "otter" and rec["source"] != "otter":
            return False
        if cur and cur["source"] == "otter" and not replace_live:
            return False
        with self._conn:
            self._conn.execute("DELETE FROM transcripts_fts WHERE otid=?", (rec["otid"],))
            self._conn.execute(
                "INSERT OR REPLACE INTO transcripts(otid, date, start_time, title, folder, link, text, duration_s, source, summary, imported_at, seen_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["otid"], rec.get("date") or "", rec.get("start_time") or "", rec.get("title") or "", rec.get("folder") or "",
                 rec.get("link") or "", rec.get("text") or "", rec.get("duration_s"), rec["source"], rec.get("summary"),
                 dt.datetime.now(self.tz).isoformat(timespec="seconds"), cur["seen_ts"] if cur else None))   # a re-import is not news
            self._conn.execute("INSERT INTO transcripts_fts(otid, title, text) VALUES (?,?,?)",
                               (rec["otid"], rec.get("title") or "", rec.get("text") or ""))
        return True

    def reindex_archive(self, force: bool = False) -> int:
        """Parse the Logseq pages. Skipped when none has changed since the last pass (unless force)."""
        if not self.archive_enabled():
            return 0
        pages = {title: self.graph_dir / "pages" / f"{title}.md" for title in ARCHIVE_PAGES}
        stamp = ",".join(f"{t}:{int(p.stat().st_mtime)}" for t, p in pages.items() if p.exists())
        if not force and stamp == self._meta("archive_stamp"):
            return 0
        n = 0
        with self._lock:
            # A recording the extension filed on a folder page often also sits on the "(no folder)" page; parse
            # the unfiled page first so the folder page's row (with its folder name) is the one that stays.
            for title, path in sorted(pages.items(), key=lambda kv: ARCHIVE_PAGES[kv[0]] != ""):
                if not path.exists():
                    continue
                for rec in parse_archive_page(path.read_text(encoding="utf-8", errors="replace"), ARCHIVE_PAGES[title]):
                    if rec["otid"] and self._upsert(rec):
                        n += 1
            with self._conn:
                self._set_meta("archive_stamp", stamp)
        log.info("otter archive: indexed %d recording(s) from %s", n, self.graph_dir)
        return n

    def _get_client(self) -> OtterClient:
        if self._client is None:
            if not self.live_enabled():
                raise SourceError("OTTER_EMAIL / OTTER_PASSWORD are not set")
            self._client = OtterClient(self.email, self.password, timeout=self.settings.sources.timeout_seconds)
        return self._client

    def sync(self, max_recordings: int = 40, all_history: bool = False) -> dict[str, Any]:
        """Pull recordings from Otter that the index lacks (or that have no transcript text yet)."""
        client = self._get_client()
        with self._lock:
            self._last_sync_attempt = self._now()
            try:
                page_size = 45
                pages = 200 if all_history else max(1, -(-max_recordings // page_size))
                listed = client.speeches(page_size=min(page_size, max_recordings) if not all_history else page_size, max_pages=pages)
                if not all_history:
                    listed = listed[:max_recordings]
                added = skipped = pending = 0
                for sp in listed:
                    otid = sp.get("otid")
                    if not otid:
                        continue
                    row = self._conn.execute("SELECT source, length(text) AS n FROM transcripts WHERE otid=?", (otid,)).fetchone()
                    if row and row["source"] == "otter" and (row["n"] or 0) > 0:
                        skipped += 1
                        continue
                    if not (sp.get("process_finished") or sp.get("is_processed") or sp.get("upload_finished")):
                        pending += 1
                        continue
                    full = client.speech(otid)
                    rec = shape_speech(sp, full, self.tz)
                    if not rec["text"]:
                        pending += 1
                        continue
                    self._upsert(rec)
                    added += 1
                with self._conn:
                    self._set_meta("last_sync", dt.datetime.now(self.tz).isoformat(timespec="seconds"))
                self._last_sync_error = None
                return {"listed": len(listed), "added": added, "already_had": skipped, "still_processing": pending}
            except SourceError as e:
                self._last_sync_error = str(e)
                log.warning("otter sync failed: %s", e)
                raise

    # ----- ingestion: recordings the agent has not read yet
    def mark_all_seen(self) -> int:
        """First run of ingestion: everything already indexed is history, not news."""
        with self._conn:
            return self._conn.execute("UPDATE transcripts SET seen_ts=? WHERE seen_ts IS NULL", (self._now(),)).rowcount

    def unseen(self, limit: int = 5) -> list[dict[str, Any]]:
        """Live recordings with text that the agent has not been given yet, oldest first."""
        rows = self._conn.execute("SELECT * FROM transcripts WHERE seen_ts IS NULL AND source='otter' AND length(text) > 0 "
                                  "ORDER BY date, start_time LIMIT ?", (max(1, limit),)).fetchall()
        return [self._shape_row(r) for r in rows]

    def mark_seen(self, otid: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE transcripts SET seen_ts=? WHERE otid=?", (self._now(), otid))

    def ensure_fresh(self) -> str | None:
        """Called before reads: index the archive if it changed, and sync from Otter if due. Returns a
        warning string for the model instead of raising, so reads over the index keep working."""
        warn = None
        try:
            self.reindex_archive()
        except Exception as e:  # noqa: BLE001
            warn = f"Logseq archive could not be indexed: {e}"
        if self.live_enabled() and self._now() - self._last_sync_attempt >= self.sync_seconds:
            try:
                self.sync()
            except SourceError as e:
                warn = f"Otter sync failed ({e}); results come from the local index only"
        elif not self.live_enabled():
            warn = "Otter login not configured; only recordings already in the local index (the Logseq archive) are visible"
        return warn

    # ----- reading
    @staticmethod
    def _shape_row(r: sqlite3.Row, preview_chars: int = 0) -> dict[str, Any]:
        d = {"otid": r["otid"], "date": r["date"], "start_time": r["start_time"], "title": r["title"] or None,
             "folder": r["folder"] or None, "words": len((r["text"] or "").split()), "link": r["link"], "source": r["source"]}
        if r["duration_s"]:
            d["minutes"] = round(r["duration_s"] / 60)
        if preview_chars:
            d["preview"] = (r["text"] or "")[:preview_chars]
        return d

    def recent(self, days: int = 7, folder: str | None = None, limit: int = 50, start_date: str | None = None) -> list[dict[str, Any]]:
        end = dt.datetime.now(self.tz).date()
        if start_date:
            start = dt.date.fromisoformat(start_date)
            end = start + dt.timedelta(days=max(1, days) - 1)
        else:
            start = end - dt.timedelta(days=max(1, days) - 1)
        q = "SELECT * FROM transcripts WHERE date BETWEEN ? AND ?"
        args: list[Any] = [start.isoformat(), end.isoformat()]
        if folder is not None:
            q += " AND lower(folder) = lower(?)"
            args.append(folder)
        q += " ORDER BY date DESC, start_time DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return [self._shape_row(r, 240) for r in self._conn.execute(q, args)]

    def get(self, otid: str, offset: int = 0, max_chars: int = 40_000) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM transcripts WHERE otid=?", (otid,)).fetchone()
        if not r:
            return None
        d = self._shape_row(r)
        text = r["text"] or ""
        d.update({"summary": r["summary"], "total_chars": len(text), "offset": offset,
                  "text": text[offset:offset + max(1000, max_chars)]})
        d["truncated"] = offset + len(d["text"]) < len(text)
        if d["truncated"]:
            d["next_offset"] = offset + len(d["text"])
        return d

    def search(self, query: str, limit: int = 10, days: int | None = None, folder: str | None = None) -> list[dict[str, Any]]:
        """FTS5 over title + text with a snippet per hit; falls back to prefix/OR terms when the query is not
        valid FTS syntax (same approach as the memory store)."""
        limit = max(1, min(limit, 50))
        where, args = "", []
        if days:
            where += " AND t.date >= ?"
            args.append((dt.datetime.now(self.tz).date() - dt.timedelta(days=days)).isoformat())
        if folder is not None:
            where += " AND lower(t.folder) = lower(?)"
            args.append(folder)
        sql = ("SELECT t.*, snippet(transcripts_fts, 2, '«', '»', ' … ', 24) AS snip, bm25(transcripts_fts) AS rank "
               "FROM transcripts_fts JOIN transcripts t ON t.otid = transcripts_fts.otid "
               f"WHERE transcripts_fts MATCH ?{where} ORDER BY rank LIMIT ?")
        try:
            rows = self._conn.execute(sql, [query, *args, limit]).fetchall()
        except sqlite3.OperationalError:
            terms = [re.sub(r"[^\w]", "", w) for w in query.split()]
            fallback = " OR ".join(f'"{w}"*' for w in terms if w) or '""'
            rows = self._conn.execute(sql, [fallback, *args, limit]).fetchall()
        out = []
        for r in rows:
            d = self._shape_row(r)
            d["snippet"] = r["snip"]
            out.append(d)
        return out
