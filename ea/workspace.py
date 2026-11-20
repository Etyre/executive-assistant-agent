"""Shared documents: Markdown pages the user and the agent both edit, apart from the conversation.

`<EA_DATA>/workspace/` holds them (so any editor works too): `workspace.md` is the whiteboard that the app shows
by default; the agent (or the user) can create more, e.g. `plan-for-dc-trip.md`. Every saved version of every
document is kept in `<EA_DATA>/state.db` (table workspace_versions, last 300 per document). Version ids are a
hash of the content, so edits made outside the app are recognised. Writers pass the version they started from;
a save over a newer version is refused with the current text so the caller can merge. Pages may contain
```mermaid fences, which the app renders as diagrams."""
from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import re
import sqlite3
import threading
import time
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_versions (
    id INTEGER PRIMARY KEY, version TEXT NOT NULL, ts REAL NOT NULL, by TEXT NOT NULL, note TEXT DEFAULT '', text TEXT NOT NULL,
    doc TEXT NOT NULL DEFAULT 'workspace'
);
"""
KEEP = 300
WHITEBOARD = "workspace"
DEFAULT = "# Workspace\n\nA whiteboard for what Eli and Atlas are working on right now. Either of us edits it; things come off when done.\n"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,80}$")


def valid_name(name: str) -> str:
    name = name.strip().removesuffix(".md").strip()
    if not _NAME.match(name) or ".." in name:
        raise ValueError("document names are letters, digits, spaces, '-', '_' and '.', up to 80 characters")
    return name


class Conflict(Exception):
    def __init__(self, current: dict[str, Any]):
        super().__init__("workspace changed since you read it")
        self.current = current


def _version(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# One lock for every use of the connection. All documents share one connection and the web server reads from several
# threads at once; two overlapping executes raised "sqlite3.InterfaceError: bad parameter or other API misuse" and the
# app's document list came back as a 500 (seen 2026-11-16).
_DB = threading.RLock()


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _DB:
        return conn.execute(sql, args).fetchall()


def _connect(db_path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(pathlib.Path(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "doc" not in {r[1] for r in conn.execute("PRAGMA table_info(workspace_versions)")}:   # before multiple documents
        conn.execute("ALTER TABLE workspace_versions ADD COLUMN doc TEXT NOT NULL DEFAULT 'workspace'")
    conn.commit()
    return conn


class Workspace:
    """One document. `Documents` hands these out; the whiteboard is `Documents.whiteboard()`."""

    def __init__(self, settings: config.Settings | None = None, path: pathlib.Path | None = None, db_path: pathlib.Path | None = None,
                 name: str = WHITEBOARD, conn: sqlite3.Connection | None = None, on_change=None, default: str | None = None):
        self.s = settings or config.load()
        self.name = name
        self.path = pathlib.Path(path or self.s.data_dir / "workspace" / f"{name}.md")
        self._conn = conn or _connect(db_path or self.s.state_db)
        self._lock = threading.Lock()
        self.on_change = on_change            # callable(info) set by the server: fan out to open apps
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = DEFAULT if default is None else default
            self.path.write_text(text, encoding="utf-8")
            self._record(text, "system", "created")
        elif not _rows(self._conn, "SELECT 1 FROM workspace_versions WHERE doc=? LIMIT 1", (self.name,)):
            self._record(self.path.read_text(encoding="utf-8"), "system", "imported")     # a file that appeared on disk

    # ------------------------------------------------------------------ reading
    def read(self) -> dict[str, Any]:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        v = _version(text)
        last = next(iter(_rows(self._conn, "SELECT ts, by, note FROM workspace_versions WHERE doc=? AND version=? ORDER BY id DESC LIMIT 1", (self.name, v))), None)
        return {"name": self.name, "text": text, "version": v, "chars": len(text),
                "updated_at": dt.datetime.fromtimestamp(last["ts"]).isoformat(timespec="seconds") if last else
                dt.datetime.fromtimestamp(self.path.stat().st_mtime).isoformat(timespec="seconds") if self.path.exists() else None,
                "updated_by": last["by"] if last else "outside the app"}

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = _rows(self._conn, "SELECT version, ts, by, note, length(text) AS chars FROM workspace_versions WHERE doc=? ORDER BY id DESC LIMIT ?", (self.name, limit))
        return [{"version": r["version"], "at": dt.datetime.fromtimestamp(r["ts"]).isoformat(timespec="seconds"), "by": r["by"],
                 "note": r["note"], "chars": r["chars"]} for r in rows]

    def version_text(self, version: str) -> str | None:
        r = next(iter(_rows(self._conn, "SELECT text FROM workspace_versions WHERE doc=? AND version=? ORDER BY id DESC LIMIT 1", (self.name, version))), None)
        return r["text"] if r else None

    # ------------------------------------------------------------------ writing
    def _record(self, text: str, by: str, note: str) -> None:
        with _DB, self._conn:
            self._conn.execute("INSERT INTO workspace_versions(version, ts, by, note, text, doc) VALUES(?,?,?,?,?,?)", (_version(text), time.time(), by, note, text, self.name))
            self._conn.execute("DELETE FROM workspace_versions WHERE doc=? AND id NOT IN (SELECT id FROM workspace_versions WHERE doc=? ORDER BY id DESC LIMIT ?)", (self.name, self.name, KEEP))

    def write(self, text: str, by: str, base_version: str | None = None, note: str = "", client_id: str = "") -> dict[str, Any]:
        """Replace the document. With base_version, refuse (Conflict) if the document moved on since then."""
        with self._lock:
            cur = self.read()
            if base_version and base_version != cur["version"]:
                raise Conflict(cur)
            if text == cur["text"]:
                return cur
            self.path.write_text(text, encoding="utf-8")
            self._record(text, by, note)
            out = self.read()
        if self.on_change:
            self.on_change({"doc": self.name, "version": out["version"], "by": by, "client_id": client_id, "note": note, "chars": out["chars"]})
        return out

    def append(self, text: str, by: str, note: str = "") -> dict[str, Any]:
        with self._lock:
            cur = self.read()["text"]
        sep = "" if not cur or cur.endswith("\n\n") else ("\n" if cur.endswith("\n") else "\n\n")
        return self.write(cur + sep + text.rstrip("\n") + "\n", by, note=note or "appended")

    def edit(self, find: str, replace: str, by: str, note: str = "") -> dict[str, Any]:
        """Replace one exact occurrence of `find` (it must occur exactly once)."""
        with self._lock:
            cur = self.read()["text"]
        n = cur.count(find)
        if n != 1:
            raise ValueError(f"the text to replace occurs {n} times; it must occur exactly once")
        return self.write(cur.replace(find, replace, 1), by, note=note or "edited")


class Documents:
    """All shared documents: the whiteboard plus any the agent or user creates."""

    def __init__(self, settings: config.Settings | None = None, folder: pathlib.Path | None = None, db_path: pathlib.Path | None = None):
        self.s = settings or config.load()
        self.folder = pathlib.Path(folder or self.s.data_dir / "workspace")
        self.folder.mkdir(parents=True, exist_ok=True)
        self._conn = _connect(db_path or self.s.state_db)
        self._docs: dict[str, Workspace] = {}
        self.on_change = None
        old = self.folder.parent / "workspace.md"                  # the single-file layout from before
        new = self.folder / f"{WHITEBOARD}.md"
        if old.exists() and not new.exists():
            old.rename(new)

    def _notify(self, info: dict[str, Any]) -> None:
        if self.on_change:
            self.on_change(info)

    def get(self, name: str = WHITEBOARD, create: bool = True, default: str | None = None) -> Workspace | None:
        name = valid_name(name)
        if name not in self._docs:
            path = self.folder / f"{name}.md"
            if not path.exists() and not create:
                return None
            self._docs[name] = Workspace(self.s, path=path, name=name, conn=self._conn, on_change=self._notify,
                                         default=default if default is not None else (DEFAULT if name == WHITEBOARD else f"# {name}\n"))
        return self._docs[name]

    def whiteboard(self) -> Workspace:
        return self.get(WHITEBOARD)

    def names(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.folder.glob("*.md"), key=lambda p: (p.stem != WHITEBOARD, p.stem.lower())):
            try:
                d = self.get(p.stem).read()
            except ValueError:           # a file put there by hand whose name the app cannot use ("Eli's notes.md",
                continue                 # a dotfile): leave it out instead of failing the whole list (2026-11-20)
            out.append({"name": p.stem, "chars": d["chars"], "updated_at": d["updated_at"], "updated_by": d["updated_by"],
                        "title": next((ln.lstrip("# ").strip() for ln in d["text"].splitlines() if ln.startswith("#")), p.stem),
                        "whiteboard": p.stem == WHITEBOARD})
        return out

    def delete(self, name: str, by: str = "user") -> bool:
        name = valid_name(name)
        if name == WHITEBOARD:
            raise ValueError("the whiteboard cannot be deleted; clear it instead")
        doc = self.get(name, create=False)
        if doc is None:
            return False
        last = doc.read()["text"]
        doc._record(last, by, "deleted")                              # the last text stays in the history
        doc.path.unlink(missing_ok=True)
        self._docs.pop(name, None)
        self._notify({"doc": name, "deleted": True, "by": by})
        return True
