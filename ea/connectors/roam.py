"""Read-only access to the user's Roam Research graph, from an export dropped into `<EA_DATA>/roam/`.

Accepted: the JSON export (Roam: ... menu -> Export All -> JSON; one `.json` per graph, the richest form:
block uids, timestamps, nesting) and/or the Markdown export (a `.zip` or a folder of `.md` files, one page each;
no timestamps, so "recent" works from journal page titles only). Everything is flattened into `<EA_DATA>/roam.db`
(pages, blocks with their parent chain, an FTS5 index) and re-indexed whenever a file in the folder changes.
Nothing is ever written back to Roam."""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import pathlib
import re
import sqlite3
import threading
import time
import zipfile
from typing import Any, Iterable

from .. import config
from ..sqlite_locked import LockedConnection

log = logging.getLogger(__name__)

_ORDINAL = re.compile(r"(\d+)(st|nd|rd|th)")
_BLOCK_REF = re.compile(r"\(\(([A-Za-z0-9_-]{6,12})\)\)")
_PAGE_REF = re.compile(r"\[\[([^\]]+)\]\]|#\[\[([^\]]+)\]\]|#([\w./-]+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    title TEXT PRIMARY KEY, uid TEXT, created REAL, edited REAL, date TEXT, blocks INTEGER DEFAULT 0, source TEXT
);
CREATE INDEX IF NOT EXISTS pages_date ON pages(date);
CREATE TABLE IF NOT EXISTS blocks (
    uid TEXT PRIMARY KEY, page TEXT NOT NULL, parent TEXT, ord INTEGER, depth INTEGER, text TEXT NOT NULL,
    created REAL, edited REAL
);
CREATE INDEX IF NOT EXISTS blocks_page ON blocks(page, ord);
CREATE INDEX IF NOT EXISTS blocks_edited ON blocks(edited);
CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(uid UNINDEXED, page, text, tokenize='unicode61');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def journal_date(title: str) -> str | None:
    """'November 6th, 2026' -> '2026-11-06' (Roam's daily-note titles); None for ordinary pages."""
    try:
        return dt.datetime.strptime(_ORDINAL.sub(r"\1", title.strip()), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _ms(v: Any) -> float | None:
    try:
        return float(v) / 1000.0 if v else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- parsing the exports
def parse_json_export(data: Any, source: str = "json") -> Iterable[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Yields (page, blocks) per page of a Roam JSON export."""
    pages = data if isinstance(data, list) else data.get("pages", [])
    for p in pages:
        title = (p.get("title") or "").strip()
        if not title:
            continue
        blocks: list[dict[str, Any]] = []
        ord_counter = [0]

        def walk(children: list[dict[str, Any]], parent: str | None, depth: int) -> None:
            for c in children or []:
                text = c.get("string")
                if text is None:
                    text = ""
                uid = c.get("uid") or f"{title}:{ord_counter[0]}"
                blocks.append({"uid": uid, "page": title, "parent": parent, "ord": ord_counter[0], "depth": depth,
                               "text": text, "created": _ms(c.get("create-time")), "edited": _ms(c.get("edit-time"))})
                ord_counter[0] += 1
                walk(c.get("children") or [], uid, depth + 1)

        walk(p.get("children") or [], None, 0)
        page = {"title": title, "uid": p.get("uid"), "created": _ms(p.get("create-time")), "edited": _ms(p.get("edit-time")),
                "date": journal_date(title), "source": source}
        yield page, blocks


def parse_markdown_page(title: str, text: str, source: str = "markdown") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A Roam Markdown-export page: '- ' bullets indented with 4 spaces (or tabs) per level."""
    blocks: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []          # (depth, uid)
    n = 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = re.match(r"^(\s*)(?:[-*]\s+)?(.*)$", raw)
        indent, body = m.group(1), m.group(2)
        depth = (len(indent.replace("\t", "    ")) // 4) if raw.lstrip().startswith(("-", "*")) else (stack[-1][0] + 1 if stack else 0)
        if not raw.lstrip().startswith(("-", "*")) and blocks:       # continuation line of the previous block
            blocks[-1]["text"] += "\n" + body
            continue
        while stack and stack[-1][0] >= depth:
            stack.pop()
        uid = f"{title}:{n}"
        blocks.append({"uid": uid, "page": title, "parent": stack[-1][1] if stack else None, "ord": n, "depth": depth,
                       "text": body, "created": None, "edited": None})
        stack.append((depth, uid))
        n += 1
    page = {"title": title, "uid": None, "created": None, "edited": None, "date": journal_date(title), "source": source}
    return page, blocks


def _md_title(name: str) -> str:
    t = pathlib.PurePosixPath(name).stem
    return t.replace("%2F", "/").replace("％", "/")       # Roam encodes "/" in page titles for file names


class Roam:
    def __init__(self, settings: config.Settings | None = None, db_path: pathlib.Path | None = None,
                 folder: pathlib.Path | None = None):
        self.s = settings or config.load()
        self.folder = pathlib.Path(folder or self.s.data_dir / "roam")
        self.db_path = pathlib.Path(db_path or self.s.data_dir / "roam.db")
        # Shared by the agent, its workers and the scheduler: see sqlite_locked. A search during a rebuild waits for it.
        self._conn = LockedConnection(self.db_path)
        self._lock = self._conn.lock
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ files
    @property
    def backup_dir(self) -> pathlib.Path:
        return self.folder / "backup"

    def export_files(self) -> list[pathlib.Path]:
        """The git backup checkout when there is one (its JSON only); otherwise whatever was dropped in the folder."""
        if not self.folder.exists():
            return []
        if (self.backup_dir / ".git").exists():
            return sorted(p for p in self.backup_dir.glob("*.json") if not p.name.startswith("."))
        out = [p for p in self.folder.rglob("*") if p.is_file() and p.suffix.lower() in (".json", ".md", ".zip")
               and not p.name.startswith(".") and self.backup_dir not in p.parents]
        return sorted(out)

    # ------------------------------------------------------------------ the git backup
    def sync_backup(self, force: bool = False) -> dict[str, Any]:
        """Clone the backup repo on first use, pull it when `roam_sync_hours` have passed, re-index on change.
        Returns what happened; never raises (the scheduler calls it)."""
        import subprocess
        url = self.s.sources.roam_backup_git
        if not url:
            return {"skipped": "no sources.roam_backup_git"}
        now = time.time()
        last = float(self._meta("backup_last_pull") or 0)
        if not force and now - last < self.s.sources.roam_sync_hours * 3600:
            return {"skipped": f"pulled {int((now - last) / 60)} min ago"}
        try:
            if not (self.backup_dir / ".git").exists():
                self.backup_dir.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "clone", "--depth", "1", "-q", url, str(self.backup_dir)], check=True, capture_output=True, text=True, timeout=600)
                action = "cloned"
            else:
                before = subprocess.run(["git", "-C", str(self.backup_dir), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
                # read-only checkout: fetch the newest commit only and move to it (a shallow pull cannot fast-forward)
                subprocess.run(["git", "-C", str(self.backup_dir), "fetch", "-q", "--depth", "1", "origin"], check=True, capture_output=True, text=True, timeout=600)
                subprocess.run(["git", "-C", str(self.backup_dir), "reset", "-q", "--hard", "FETCH_HEAD"], check=True, capture_output=True, text=True, timeout=120)
                after = subprocess.run(["git", "-C", str(self.backup_dir), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
                action = "updated" if before != after else "unchanged"
        except subprocess.CalledProcessError as e:
            log.warning("roam backup %s failed: %s", "pull", (e.stderr or e.stdout or "")[:300])
            return {"error": (e.stderr or e.stdout or str(e))[:300]}
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("roam backup sync failed: %s", e)
            return {"error": str(e)[:300]}
        with self._conn:
            self._set_meta("backup_last_pull", str(now))
        commit = subprocess.run(["git", "-C", str(self.backup_dir), "log", "-1", "--format=%ci"], capture_output=True, text=True).stdout.strip()
        with self._conn:
            self._set_meta("backup_commit_date", commit)
        r = self.reindex()                                        # no-op unless the files changed
        log.info("roam backup %s (commit %s); index %s", action, commit, "rebuilt" if r.get("reindexed") else "unchanged")
        return {"action": action, "commit": commit, "reindexed": r.get("reindexed", False)}

    def enabled(self) -> bool:
        return bool(self.export_files())

    def _signature(self) -> str:
        return json.dumps([(str(p.relative_to(self.folder)), p.stat().st_mtime, p.stat().st_size) for p in self.export_files()])

    def _meta(self, key: str) -> str | None:
        r = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ------------------------------------------------------------------ indexing
    def _iter_pages(self) -> Iterable[tuple[dict[str, Any], list[dict[str, Any]]]]:
        for p in self.export_files():
            try:
                if p.suffix.lower() == ".json":
                    yield from parse_json_export(json.loads(p.read_text(encoding="utf-8")), source=p.name)
                elif p.suffix.lower() == ".md":
                    yield parse_markdown_page(_md_title(p.name), p.read_text(encoding="utf-8"), source=p.name)
                elif p.suffix.lower() == ".zip":
                    with zipfile.ZipFile(p) as z:
                        for name in z.namelist():
                            if name.lower().endswith(".md") and not name.startswith("__MACOSX"):
                                yield parse_markdown_page(_md_title(name), z.read(name).decode("utf-8", "replace"), source=f"{p.name}:{name}")
                            elif name.lower().endswith(".json"):
                                yield from parse_json_export(json.loads(z.read(name).decode("utf-8")), source=f"{p.name}:{name}")
            except (OSError, ValueError, zipfile.BadZipFile) as e:
                log.warning("roam: could not read %s: %s", p, e)

    def reindex(self, force: bool = False) -> dict[str, Any]:
        sig = self._signature()
        if not force and sig == self._meta("signature"):
            return {"reindexed": False, **self.status()}
        with self._lock:
            c = self._conn
            c.execute("DELETE FROM pages"); c.execute("DELETE FROM blocks"); c.execute("DELETE FROM blocks_fts")
            seen_pages: dict[str, str] = {}
            n_pages = n_blocks = 0
            for page, blocks in self._iter_pages():
                title = page["title"]
                # a JSON export beats a Markdown copy of the same page (it has uids and timestamps)
                if title in seen_pages and not (page["source"].endswith(".json") and not seen_pages[title].endswith(".json")):
                    continue
                if title in seen_pages:
                    c.execute("DELETE FROM blocks WHERE page=?", (title,)); c.execute("DELETE FROM blocks_fts WHERE page=?", (title,))
                seen_pages[title] = page["source"]
                c.execute("INSERT OR REPLACE INTO pages(title,uid,created,edited,date,blocks,source) VALUES(?,?,?,?,?,?,?)",
                          (title, page["uid"], page["created"], page["edited"], page["date"], len(blocks), page["source"]))
                for b in blocks:
                    c.execute("INSERT OR REPLACE INTO blocks(uid,page,parent,ord,depth,text,created,edited) VALUES(?,?,?,?,?,?,?,?)",
                              (b["uid"], title, b["parent"], b["ord"], b["depth"], b["text"], b["created"], b["edited"]))
                    if b["text"].strip():
                        c.execute("INSERT INTO blocks_fts(uid,page,text) VALUES(?,?,?)", (b["uid"], title, b["text"]))
                n_pages += 1
                n_blocks += len(blocks)
            self._set_meta("signature", sig)
            self._set_meta("indexed_at", dt.datetime.now().isoformat(timespec="seconds"))
            c.commit()
        log.info("roam: indexed %d pages, %d blocks from %s", n_pages, n_blocks, self.folder)
        return {"reindexed": True, **self.status()}

    def ensure_fresh(self) -> str | None:
        """Re-index if the export changed. Returns a warning when there is nothing to read."""
        if not self.enabled():
            return (f"no Roam export found in {self.folder}: drop the JSON export (Export All -> JSON) or the Markdown "
                    "export (.zip or .md files) there")
        try:
            self.reindex()
        except sqlite3.Error as e:
            return f"roam index error: {e}"
        return None

    def status(self) -> dict[str, Any]:
        pages = self._conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        blocks = self._conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        journals = self._conn.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM pages WHERE date IS NOT NULL").fetchone()
        last_edit = self._conn.execute("SELECT MAX(edited) FROM blocks").fetchone()[0]
        return {"folder": str(self.folder), "files": [p.name for p in self.export_files()], "pages": pages, "blocks": blocks,
                "backup": {"git": self.s.sources.roam_backup_git or None, "commit_date": self._meta("backup_commit_date"),
                           "last_pull": dt.datetime.fromtimestamp(float(self._meta("backup_last_pull"))).isoformat(timespec="minutes") if self._meta("backup_last_pull") else None},
                "journal_pages": journals[2], "first_journal": journals[0], "last_journal": journals[1],
                "last_block_edit": dt.datetime.fromtimestamp(last_edit).isoformat(timespec="minutes") if last_edit else None,
                "indexed_at": self._meta("indexed_at")}

    # ------------------------------------------------------------------ reading
    def _resolve_refs(self, text: str, depth: int = 0) -> str:
        """Inline ((block refs)) so a page reads as the user sees it."""
        if depth > 2:
            return text

        def sub(m: re.Match) -> str:
            r = self._conn.execute("SELECT text FROM blocks WHERE uid=?", (m.group(1),)).fetchone()
            return self._resolve_refs(r["text"], depth + 1) if r else m.group(0)
        return _BLOCK_REF.sub(sub, text)

    def _breadcrumb(self, uid: str, max_parents: int = 3) -> list[str]:
        out: list[str] = []
        cur = self._conn.execute("SELECT parent FROM blocks WHERE uid=?", (uid,)).fetchone()
        while cur and cur["parent"] and len(out) < max_parents:
            r = self._conn.execute("SELECT text, parent FROM blocks WHERE uid=?", (cur["parent"],)).fetchone()
            if not r:
                break
            out.append(r["text"][:80])
            cur = r
        return list(reversed(out))

    def page(self, title: str, offset: int = 0, max_chars: int = 40_000) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM pages WHERE title=? COLLATE NOCASE", (title,)).fetchone()
        if not r:
            like = self._conn.execute("SELECT title FROM pages WHERE title LIKE ? COLLATE NOCASE ORDER BY blocks DESC LIMIT 8", (f"%{title}%",)).fetchall()
            return {"error": f"no page titled {title!r}", "similar": [x["title"] for x in like]} if like else None
        lines = []
        for b in self._conn.execute("SELECT depth, text, uid FROM blocks WHERE page=? ORDER BY ord", (r["title"],)):
            lines.append("  " * b["depth"] + "- " + self._resolve_refs(b["text"]).replace("\n", "\n" + "  " * (b["depth"] + 1)))
        text = "\n".join(lines)
        chunk = text[offset:offset + max_chars]
        out = {"title": r["title"], "date": r["date"], "blocks": r["blocks"], "text": chunk,
               "edited": dt.datetime.fromtimestamp(r["edited"]).isoformat(timespec="minutes") if r["edited"] else None}
        if offset + max_chars < len(text):
            out["next_offset"] = offset + max_chars
            out["total_chars"] = len(text)
        return out

    def search(self, query: str, limit: int = 10, days: int | None = None, page: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        where, args = "", []
        if days:
            since = (dt.datetime.now() - dt.timedelta(days=days))
            where += " AND (b.edited >= ? OR p.date >= ?)"
            args += [since.timestamp(), since.date().isoformat()]
        if page:
            where += " AND p.title LIKE ? COLLATE NOCASE"
            args.append(f"%{page}%")
        sql = ("SELECT b.uid, b.page, b.text, b.edited, p.date, snippet(blocks_fts, 2, '«', '»', ' … ', 24) AS snip, bm25(blocks_fts) AS rank "
               "FROM blocks_fts JOIN blocks b ON b.uid = blocks_fts.uid JOIN pages p ON p.title = b.page "
               f"WHERE blocks_fts MATCH ?{where} ORDER BY rank LIMIT ?")
        try:
            rows = self._conn.execute(sql, [query, *args, limit]).fetchall()
        except sqlite3.OperationalError:
            terms = [re.sub(r"[^\w]", "", w) for w in query.split()]
            fallback = " OR ".join(f'"{w}"*' for w in terms if w) or '""'
            rows = self._conn.execute(sql, [fallback, *args, limit]).fetchall()
        out = []
        for r in rows:
            out.append({"page": r["page"], "date": r["date"], "snippet": r["snip"], "text": self._resolve_refs(r["text"])[:600],
                        "context": self._breadcrumb(r["uid"]), "uid": r["uid"],
                        "edited": dt.datetime.fromtimestamp(r["edited"]).isoformat(timespec="minutes") if r["edited"] else None})
        return out

    def recent(self, days: int = 7, limit: int = 40, start_date: str | None = None) -> dict[str, Any]:
        """Journal pages in the window (previews), plus the pages with the most recently edited blocks."""
        end = dt.date.fromisoformat(start_date) + dt.timedelta(days=days) if start_date else dt.date.today() + dt.timedelta(days=1)
        start = dt.date.fromisoformat(start_date) if start_date else dt.date.today() - dt.timedelta(days=days)
        journals = []
        for r in self._conn.execute("SELECT title, date, blocks FROM pages WHERE date >= ? AND date < ? ORDER BY date DESC LIMIT ?",
                                    (start.isoformat(), end.isoformat(), limit)):
            first = self._conn.execute("SELECT text FROM blocks WHERE page=? ORDER BY ord LIMIT 4", (r["title"],)).fetchall()
            journals.append({"title": r["title"], "date": r["date"], "blocks": r["blocks"],
                             "preview": " / ".join(self._resolve_refs(x["text"])[:120] for x in first)})
        edited = []
        since = dt.datetime.combine(start, dt.time()).timestamp()
        for r in self._conn.execute("SELECT page, COUNT(*) AS n, MAX(edited) AS last FROM blocks WHERE edited >= ? GROUP BY page ORDER BY last DESC LIMIT ?",
                                    (since, limit)):
            edited.append({"page": r["page"], "blocks_edited": r["n"], "last": dt.datetime.fromtimestamp(r["last"]).isoformat(timespec="minutes")})
        return {"from": start.isoformat(), "to": end.isoformat(), "journal_pages": journals, "recently_edited_pages": edited}

    def pages(self, contains: str = "", limit: int = 50, journals: bool = False) -> list[dict[str, Any]]:
        where = "WHERE date IS NULL" if not journals else "WHERE date IS NOT NULL"
        args: list[Any] = []
        if contains:
            where += " AND title LIKE ? COLLATE NOCASE"
            args.append(f"%{contains}%")
        rows = self._conn.execute(f"SELECT title, date, blocks, edited FROM pages {where} ORDER BY blocks DESC, title LIMIT ?", [*args, max(1, min(limit, 500))])
        return [{"title": r["title"], "date": r["date"], "blocks": r["blocks"],
                 "edited": dt.datetime.fromtimestamp(r["edited"]).isoformat(timespec="minutes") if r["edited"] else None} for r in rows]

    def backlinks(self, title: str, limit: int = 40) -> list[dict[str, Any]]:
        """Blocks anywhere that reference [[title]] or #title (what Roam shows under 'Linked References')."""
        pats = [f"%[[{title}]]%", f"%#{title}%", f"%#[[{title}]]%"]
        rows = self._conn.execute("SELECT b.uid, b.page, b.text, b.edited, p.date FROM blocks b JOIN pages p ON p.title=b.page "
                                  "WHERE (b.text LIKE ? OR b.text LIKE ? OR b.text LIKE ?) AND b.page != ? COLLATE NOCASE "
                                  "ORDER BY COALESCE(b.edited, 0) DESC, p.date DESC LIMIT ?", [*pats, title, max(1, min(limit, 200))]).fetchall()
        return [{"page": r["page"], "date": r["date"], "text": self._resolve_refs(r["text"])[:600], "context": self._breadcrumb(r["uid"]),
                 "edited": dt.datetime.fromtimestamp(r["edited"]).isoformat(timespec="minutes") if r["edited"] else None} for r in rows]
