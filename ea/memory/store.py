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
import re
import sqlite3
from typing import Any

import yaml

_FM = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:80] or "memory"


def _today() -> str:
    return dt.date.today().isoformat()


class Memory(dict):
    """name, title, created, sources, edits, tags, body, path."""


class MemoryStore:
    def __init__(self, directory: pathlib.Path, index_path: pathlib.Path | None = None):
        self.dir = pathlib.Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "_archive").mkdir(exist_ok=True)
        self.index_path = index_path or self.dir / ".index.sqlite"
        self._conn = sqlite3.connect(self.index_path, check_same_thread=False)
        self._conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5(name UNINDEXED, title, tags, body)")
        self._conn.commit()
        self.reindex()

    # ------------------------------------------------------------------ files
    def _path(self, name: str) -> pathlib.Path:
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
    def list(self) -> list[Memory]:
        out = []
        for p in sorted(self.dir.glob("*.md")):
            out.append(self.parse(p.read_text(), p.stem, p))
        return out

    def read(self, name: str) -> Memory | None:
        p = self._path(name)
        if not p.exists():
            return None
        return self.parse(p.read_text(), name, p)

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

    def archive(self, name: str) -> bool:
        p = self._path(name)
        if not p.exists():
            return False
        p.rename(self.dir / "_archive" / f"{name}.{_today()}.md")
        self._conn.execute("DELETE FROM mem WHERE name=?", (name,))
        self._conn.commit()
        return True

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
