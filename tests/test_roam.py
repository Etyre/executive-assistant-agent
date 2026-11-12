"""Roam: parsing, the reader, and the daily git backup sync (with a local bare repo)."""
import json
import pathlib
import subprocess
import tempfile

from ea import config
from ea.connectors.roam import Roam, journal_date, parse_markdown_page


def test_journal_dates_and_markdown_pages():
    assert journal_date("November 6th, 2026") == "2026-11-06" and journal_date("Open Loops") is None
    page, blocks = parse_markdown_page("Plan", "- a\n    - b\n- c\n")
    assert [(b["depth"], b["text"]) for b in blocks] == [(0, "a"), (1, "b"), (0, "c")] and blocks[1]["parent"] == blocks[0]["uid"]


def _export(pages):
    return [{"title": t, "uid": t[:5], "create-time": 1700000000000, "edit-time": 1700000000000,
             "children": [{"string": s, "uid": f"{t[:3]}{i}", "create-time": 1700000000000, "edit-time": 1700000000000} for i, s in enumerate(body)]}
            for t, body in pages]


def test_git_backup_is_cloned_pulled_and_reindexed(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    d = pathlib.Path(tempfile.mkdtemp())
    src = d / "src"; src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True); subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    (src / "eli.json").write_text(json.dumps(_export([("November 6th, 2026", ["call the carpet guy"]), ("Open Loops", ["taxes"])])))
    subprocess.run(["git", "-C", str(src), "add", "."], check=True); subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "first"], check=True)
    s.sources.roam_backup_git = str(src)
    s.sources.roam_sync_hours = 1
    roam = Roam(s, db_path=d / "roam.db", folder=d / "roam")
    (d / "roam").mkdir(); (d / "roam" / "old-drop.md").write_text("- stale drop\n")     # ignored once the backup exists
    r = roam.sync_backup()
    assert r.get("action") == "cloned" and r["reindexed"] is True, r
    assert roam.status()["pages"] == 2 and roam.search("carpet")[0]["page"] == "November 6th, 2026"
    assert roam.search("stale") == []                                                    # the drop folder is no longer read
    assert roam.sync_backup()["skipped"].startswith("pulled")                           # not due yet
    (src / "eli.json").write_text(json.dumps(_export([("November 6th, 2026", ["carpet done"]), ("Open Loops", ["taxes"]), ("New page", ["hello"])])))
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-am", "second"], check=True)
    r = roam.sync_backup(force=True)
    assert r["action"] == "updated" and r["reindexed"] is True and roam.status()["pages"] == 3
    assert roam.search("done")[0]["text"] == "carpet done" and roam.status()["backup"]["commit_date"]
    assert roam.sync_backup(force=True)["action"] == "unchanged"
