"""One SQLite connection that several threads may use at once.

The agent's stores each keep a single connection and are called from the turn threads, background workers, the
scheduler and the web server together. A bare connection used that way fails now and then with "bad parameter or
other API misuse", or hands one thread another's half-read cursor (the workspace store on 2026-11-12, then the
conversation, memory and device stores on 11-20). Here every statement runs under one lock and its rows are
fetched before the lock is released; `with conn:` holds the lock for a whole transaction, so a reader never sees a
write that is half done (the Roam index being rebuilt, for example). `conn.lock` is re-entrant and is the lock a
store should use for its own multi-step operations.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable


class Rows(list):
    """The rows of one statement, already fetched, with the cursor attributes callers use."""
    rowcount: int = -1
    lastrowid: int | None = None

    def fetchall(self) -> "Rows":
        return self

    def fetchone(self) -> Any:
        return self[0] if self else None


class LockedConnection:
    def __init__(self, path: Any, **kw: Any):
        self._c = sqlite3.connect(path, check_same_thread=False, **kw)
        self.lock = threading.RLock()

    @property
    def row_factory(self) -> Any:
        return self._c.row_factory

    @row_factory.setter
    def row_factory(self, f: Any) -> None:
        self._c.row_factory = f

    def _done(self, cur: sqlite3.Cursor) -> Rows:
        rows = Rows(cur.fetchall() if cur.description else [])
        rows.rowcount, rows.lastrowid = cur.rowcount, cur.lastrowid
        return rows

    def execute(self, sql: str, args: Any = ()) -> Rows:
        with self.lock:
            return self._done(self._c.execute(sql, args))

    def executemany(self, sql: str, seq: Iterable[Any]) -> Rows:
        with self.lock:
            return self._done(self._c.executemany(sql, seq))

    def executescript(self, script: str) -> None:
        with self.lock:
            self._c.executescript(script)

    def commit(self) -> None:
        with self.lock:
            self._c.commit()

    def rollback(self) -> None:
        with self.lock:
            self._c.rollback()

    def close(self) -> None:
        with self.lock:
            self._c.close()

    def __enter__(self) -> "LockedConnection":
        self.lock.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                self._c.commit()
            else:
                self._c.rollback()
        finally:
            self.lock.release()
