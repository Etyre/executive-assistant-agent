"""The connection the Roam and Otter indexes share between threads."""
import threading

from ea.sqlite_locked import LockedConnection


def test_readers_never_see_a_half_done_transaction_and_nothing_raises(tmp_path):
    c = LockedConnection(tmp_path / "x.db")
    c.execute("CREATE TABLE t (n INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    c.commit()
    bad, stop = [], threading.Event()

    def read():
        while not stop.is_set():
            try:
                n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
                if n != 100:
                    bad.append(n)
            except Exception as e:  # noqa: BLE001
                bad.append(repr(e))

    def rebuild():
        for _ in range(40):
            with c:                                   # what a reindex does: empty the table, fill it again
                c.execute("DELETE FROM t")
                c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    readers = [threading.Thread(target=read) for _ in range(3)]
    [t.start() for t in readers]
    rebuild(); stop.set(); [t.join() for t in readers]
    assert bad == []


def test_rows_carry_what_callers_ask_a_cursor_for(tmp_path):
    c = LockedConnection(tmp_path / "x.db")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    r = c.execute("INSERT INTO t (v) VALUES (?)", ("a",))
    assert r.lastrowid == 1 and r.rowcount == 1 and r.fetchone() is None
    c.commit()
    try:
        with c:
            c.execute("INSERT INTO t (v) VALUES ('b')")
            raise RuntimeError("fails midway")
    except RuntimeError:
        pass
    assert [tuple(x) for x in c.execute("SELECT v FROM t")] == [("a",)]       # rolled back, and the lock was released
