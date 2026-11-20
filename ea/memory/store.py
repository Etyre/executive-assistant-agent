"""The agent's scratch space: one Markdown file per memory, a full-text index over them.

Files are the source of truth (human-readable, diffable, trivially moved to another machine); the SQLite
FTS5 index is a cache rebuilt from them. Every memory records the date (and channel) of the conversation
it came from, and every edit appends the date it was made, so any memory can be traced back to the
transcript of the day that produced it (see transcripts/YYYY-MM-DD.jsonl).

Frontmatter:
    ---
    title: Morning routine timing
    created: 2026-11-04
    sources:            # conversations this memory was drawn from, never removed
      - 2026-11-04 telegram
      - 2026-11-09 voice
    edits:              # when the body was changed (the first entry is the creation)
      - 2026-11-04
      - 2026-11-09
    tags: [habits, mornings]
    ---
    body...
"""
from __future__ import annotations

import datetime as dt
import pathlib
import functools
import re
import sqlite3
import threading
from typing import Any

import yaml

_FM = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:80] or "memory"


def _today() -> str:
    return dt.date.today().isoformat()


def _locked(fn):
    """One SQLite connection and one folder, used by the main agent, its workers and the web server's threads at
    once. Without this, overlapping calls failed with "bad parameter or other API misuse" (found 2026-11-20; the
    same fault as the workspace store's on 11-12)."""
    @functools.wraps(fn)
    def run(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return run


class Memory(dict):
    """name, title, created, sources, edits, tags, body, path."""


class MemoryStore:
    def __init__(self, directory: pathlib.Path, index_path: pathlib.Path | None = None):
        self.dir = pathlib.Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "_archive").mkdir(exist_ok=True)
        self.index_path = index_path or self.dir / ".index.sqlite"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.index_path, check_same_thread=False)
        self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5(name UNINDEXED, title, tags, body)")
        self._conn.commit()
        self.reindex()

    # ------------------------------------------------------------------ files
    def _path(self, name: str) -> pathlib.Path:
        # A name is a file name in this folder and nothing else: "../workspace/x" used to read and write outside it.
        if not isinstance(name, str) or not name.strip() or name.startswith(".") or any(c in name for c in "/\\\0"):
            raise ValueError(f"not a memory name: {name!r} (names are plain, like 'project-notes')")
        return self.dir / f"{name}.md"

    @staticmethod
    def parse(text: str, name: str, path: pathlib.Path | None = None) -> Memory:
        m = _FM.match(text)
        meta: dict[str, Any] = {}
        body = text
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError:
                meta = {}
            body = m.group(2)
        return Memory(name=name, title=str(meta.get("title") or name), created=str(meta.get("created") or ""),
                      sources=[str(x) for x in (meta.get("sources") or [])],
                      edits=[str(x) for x in (meta.get("edits") or [])],
                      tags=[str(x) for x in (meta.get("tags") or [])], body=body.strip("\n"), path=str(path or ""))

    @staticmethod
    def render(mem: Memory) -> str:
        meta = {"title": mem["title"], "created": mem["created"], "sources": mem["sources"],
                "edits": mem["edits"], "tags": mem["tags"]}
        fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=None).strip()
        return f"---\n{fm}\n---\n{mem['body'].strip()}\n"

    def _save(self, mem: Memory) -> None:
        p = self._path(mem["name"])
        p.write_text(self.render(mem))
        mem["path"] = str(p)
        self._index_one(mem)

    # ------------------------------------------------------------------ index
    def _index_one(self, mem: Memory) -> None:
        self._conn.execute("DELETE FROM mem WHERE name=?", (mem["name"],))
        self._conn.execute("INSERT INTO mem(name,title,tags,body) VALUES(?,?,?,?)",
                           (mem["name"], mem["title"], " ".join(mem["tags"]), mem["body"]))
        self._conn.commit()

    @_locked
    def reindex(self) -> int:
        self._conn.execute("DELETE FROM mem")
        n = 0
        for p in sorted(self.dir.glob("*.md")):
            mem = self.parse(p.read_text(), p.stem, p)
            self._conn.execute("INSERT INTO mem(name,title,tags,body) VALUES(?,?,?,?)",
                               (mem["name"], mem["title"], " ".join(mem["tags"]), mem["body"]))
            n += 1
        self._conn.commit()
        return n

    # ------------------------------------------------------------------ API
    @_locked
    def list(self) -> list[Memory]:
        out = []
        for p in sorted(self.dir.glob("*.md")):
            out.append(self.parse(p.read_text(), p.stem, p))
        return out

    @_locked
    def read(self, name: str) -> Memory | None:
        p = self._path(name)
        if not p.exists():
            return None
        return self.parse(p.read_text(), name, p)

    @_locked
    def write(self, title: str, body: str, tags: list[str] | None = None, source_date: str | None = None,
              channel: str = "", name: str | None = None) -> Memory:
        """Create a memory. If `name` (or the slug of `title`) already exists, this becomes an update."""
        name = name or slugify(title)
        if self.read(name):
            return self.update(name, body=body, tags=tags, source_date=source_date, channel=channel, title=title)
        date = source_date or _today()
        src = f"{date} {channel}".strip()
        mem = Memory(name=name, title=title, created=date, sources=[src], edits=[_today()], tags=tags or [], body=body)
        self._save(mem)
        return mem

    @_locked
    def update(self, name: str, body: str | None = None, append: str | None = None, tags: list[str] | None = None,
               source_date: str | None = None, channel: str = "", title: str | None = None) -> Memory:
        """Change a memory, keeping every earlier source and edit date and adding today's."""
        mem = self.read(name)
        if not mem:
            raise KeyError(name)
        if body is not None:
            mem["body"] = body
        if append:
            mem["body"] = (mem["body"].rstrip() + "\n\n" + append.strip()).strip()
        if tags is not None:
            mem["tags"] = sorted(set(mem["tags"]) | set(tags))
        if title:
            mem["title"] = title
        src = f"{source_date or _today()} {channel}".strip()
        if src not in mem["sources"]:
            mem["sources"].append(src)
        if not mem["edits"] or mem["edits"][-1] != _today():
            mem["edits"].append(_today())
        self._save(mem)
        return mem

    @_locked
    def archive(self, name: str) -> bool:
        p = self._path(name)
        if not p.exists():
            return False
        target = self.dir / "_archive" / f"{name}.{_today()}.md"
        if target.exists():              # archived once already today: keep both ("never deleted"), the later one timed
            target = self.dir / "_archive" / f"{name}.{_today()}-{dt.datetime.now():%H%M%S%f}.md"
        p.rename(target)
        self._conn.execute("DELETE FROM mem WHERE name=?", (name,))
        self._conn.commit()
        return True

    @_locked
    def archived(self) -> list[dict[str, Any]]:
        """Archived memories, newest first: name, archived_on, title, file. A name archived on several days is listed once per day."""
        out = []
        for p in (self.dir / "_archive").glob("*.md"):
            name, _, day = p.stem.rpartition(".")
            if name:
                out.append({"name": name, "archived_on": day, "title": self.parse(p.read_text(), name)["title"], "file": p.name})
        return sorted(out, key=lambda a: (a["archived_on"], a["name"]), reverse=True)

    @_locked
    def restore(self, name: str, archived_on: str | None = None) -> Memory | None:
        """Bring an archived memory back into the active set (the latest archived copy unless a day is given).
        Raises ValueError when an active memory already has that name: nothing is ever overwritten."""
        copies = [a for a in self.archived() if a["name"] == name and archived_on in (None, a["archived_on"])]
        if not copies:
            return None
        if self._path(name).exists():
            raise ValueError(f"an active memory named {name!r} already exists; archive or rename it first")
        (self.dir / "_archive" / copies[0]["file"]).rename(self._path(name))
        mem = self.parse(self._path(name).read_text(), name, self._path(name))
        self._index_one(mem)
        return mem

    @_locked
    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Full-text search; falls back to a prefix/OR query when the raw query is not valid FTS syntax."""
        q = query.strip()
        if not q:
            return []
        rows: list[tuple] = []
        for candidate in (q, " OR ".join(f'"{w}"' for w in re.findall(r"\w+", q)) or '""'):
            try:
                rows = self._conn.execute(
                    "SELECT name, title, snippet(mem, 3, '[', ']', '…', 24), bm25(mem) FROM mem WHERE mem MATCH ? "
                    "ORDER BY bm25(mem) LIMIT ?", (candidate, limit)).fetchall()
                break
            except sqlite3.OperationalError:
                continue
        return [{"name": r[0], "title": r[1], "snippet": r[2], "score": round(r[3], 3)} for r in rows]

    @_locked
    def digest(self, max_chars: int = 6000) -> str:
        """A compact table of contents for the system prompt: name, title, tags, last edit."""
        lines = []
        for m in self.list():
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            last = m["edits"][-1] if m["edits"] else m["created"]
            lines.append(f"- {m['name']}: {m['title']}{tags} (edited {last})")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0] + f"\n- … ({len(lines)} memories in all; use memory_search)"
        return text or "(no memories yet)"
