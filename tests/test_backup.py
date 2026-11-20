"""The Mac-side backup of the server: what counts as a good copy, and which days are kept. No network."""
import datetime as dt
import sqlite3
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bridge"))
import backup_server as b  # noqa: E402


def snapshot(root: pathlib.Path, day: str, messages: int = 2) -> pathlib.Path:
    snap = root / day
    (snap / ".backup").mkdir(parents=True)
    (snap / "transcripts").mkdir()
    (snap / "memory").mkdir()
    db = sqlite3.connect(snap / ".backup/state.db")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, ts REAL)")
    db.executemany("INSERT INTO messages (ts) VALUES (?)", [(1795000000.0 + i,) for i in range(messages)])
    db.commit(); db.close()
    (snap / "transcripts" / f"{day}.jsonl").write_text("{}\n")
    (snap / ".complete").write_text("x")
    return snap


def test_a_good_copy_is_counted_and_an_empty_or_broken_one_is_refused(tmp_path):
    good = snapshot(tmp_path, "2026-11-19")
    facts = b.check(good)
    assert facts["messages"] == 2 and facts["transcript_days"] == 1 and facts["last_day"] == "2026-11-19"
    with pytest.raises(RuntimeError, match="empty"):
        b.check(snapshot(tmp_path, "2026-11-18", messages=0))
    (good / ".backup/state.db").write_bytes(b"not a database at all" * 100)
    with pytest.raises(Exception):
        b.check(good)


def test_only_folders_that_passed_their_checks_count_as_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "SNAPS", tmp_path)
    snapshot(tmp_path, "2026-11-18"); half = snapshot(tmp_path, "2026-11-19")
    (half / ".complete").unlink()                      # a run that died midway
    assert [p.name for p in b.complete()] == ["2026-11-18"]


def test_status_says_overdue_after_thirty_hours(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(b, "SNAPS", tmp_path); monkeypatch.setattr(b, "LAST_OK", tmp_path / "last_ok.json")
    assert "no backup" in b.status()
    row = {"messages": 5, "first_day": "2026-11-04", "last_day": "2026-11-19", "memory_files": 1, "session_files": 1}
    b.LAST_OK.write_text(json.dumps({**row, "at": (dt.datetime.now() - dt.timedelta(hours=2)).isoformat()}))
    assert "OVERDUE" not in b.status()
    b.LAST_OK.write_text(json.dumps({**row, "at": (dt.datetime.now() - dt.timedelta(hours=40)).isoformat()}))
    assert "OVERDUE" in b.status()
