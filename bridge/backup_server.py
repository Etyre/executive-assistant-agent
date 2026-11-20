"""Copies the agent's conversations and memory from the server to this Mac (Eli, 2026-11-20: "we should be saving it
and backing it up, including locally"). Run by launchd (com.elityre.ea-backup) at login and every six hours; by hand:

    .venv/bin/python bridge/backup_server.py            # one backup now
    .venv/bin/python bridge/backup_server.py --status   # when the last good one was, and what it holds

What is copied from /opt/atlas/data: transcripts/, memory/, workspace/, images/, the small state files, a consistent
snapshot of state.db (taken with SQLite's own backup, not a copy of the live file), the Claude CLI's session files
and the LLM inbox. Left out because their sources rebuild them: the Roam checkout and its index, the Otter index, the
document copies. Left out because they are secrets, which the Mac holds already: .env, the push key, the ssh key.

Where: ~/Library/Application Support/Atlas/server-backup/snapshots/YYYY-MM-DD/, one folder per day. Unchanged files
are hard links to the day before, so a day costs only what changed. KEEP_DAYS days are kept. last_ok.json beside
them records the last backup that passed its checks. Log: ~/Library/Logs/ea-backup.log.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys

SERVER = "atlas-server"
DATA = "/opt/atlas/data"
ROOT = pathlib.Path.home() / "Library/Application Support/Atlas/server-backup"
SNAPS = ROOT / "snapshots"
LAST_OK = ROOT / "last_ok.json"
KEEP_DAYS = 30
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]

# The live database is never copied as a file (a copy taken mid-write can be unreadable); this writes a clean one.
SNAPSHOT_DB = (
    "import os, sqlite3; os.makedirs('/data/.backup', exist_ok=True); "
    "s = sqlite3.connect('file:/data/state.db?mode=ro', uri=True); d = sqlite3.connect('/data/.backup/state.db.tmp'); "
    "s.backup(d); d.close(); s.close(); os.replace('/data/.backup/state.db.tmp', '/data/.backup/state.db')"
)
TOP_EXCLUDES = ["/home", "/roam", "/roam.db*", "/otter.db*", "/docs", "/.env", "*.p8", "/state.db*", "/.backup/*.tmp"]
PARTS = [  # (path on the server under DATA, excludes)
    ("", TOP_EXCLUDES),
    ("home/.claude/projects", []),
    ("home/Library/Application Support/LLMInbox", []),
]


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def run(cmd: list[str], what: str, timeout: int = 1800) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}")


def complete() -> list[pathlib.Path]:
    return sorted(p for p in SNAPS.glob("20??-??-??") if (p / ".complete").exists())


def check(snap: pathlib.Path) -> dict:
    """The copy has to be usable, not merely present: the database opens and is sound, and today's talk is in it."""
    db = sqlite3.connect(f"file:{snap / '.backup/state.db'}?mode=ro", uri=True)
    try:
        ok = db.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"the copied state.db fails its integrity check: {ok}")
        messages, newest = db.execute("SELECT COUNT(*), MAX(ts) FROM messages").fetchone()
    finally:
        db.close()
    days = sorted(p.stem for p in (snap / "transcripts").glob("*.jsonl"))
    if not messages or not days:
        raise RuntimeError(f"the copy looks empty: {messages} messages, {len(days)} transcript days")
    return {
        "messages": messages,
        "newest_message": dt.datetime.fromtimestamp(newest).isoformat(timespec="seconds") if newest else None,
        "transcript_days": len(days), "first_day": days[0], "last_day": days[-1],
        "memory_files": len(list((snap / "memory").glob("*.md"))),
        "session_files": len(list((snap / "home/.claude/projects").rglob("*.jsonl"))),
    }


def backup() -> dict:
    SNAPS.mkdir(parents=True, exist_ok=True)
    today = SNAPS / dt.date.today().isoformat()
    earlier = [p for p in complete() if p != today]
    run(SSH + [SERVER, f"docker exec atlas python -c \"{SNAPSHOT_DB}\""], "the state.db snapshot on the server", 300)
    (today / ".complete").unlink(missing_ok=True)          # until this run passes its checks, the folder is not a backup
    for part, excludes in PARTS:
        dest = today / part
        dest.mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", "-a", "--delete", "-e", " ".join(SSH)] + [f"--exclude={e}" for e in excludes]
        if earlier:
            cmd.append(f"--link-dest={earlier[-1] / part}")
        src = f"{DATA}/{part}/".replace("//", "/").replace(" ", "\\ ")
        run(cmd + [f"{SERVER}:{src}", f"{dest}/"], f"copying {part or 'the data folder'}")
    facts = check(today)
    facts["at"] = dt.datetime.now().isoformat(timespec="seconds")
    facts["snapshot"] = str(today)
    (today / ".complete").write_text(facts["at"])
    LAST_OK.write_text(json.dumps(facts, indent=1))
    for old in complete()[:-KEEP_DAYS]:                    # only ever counted among good ones, so a bad week deletes nothing
        shutil.rmtree(old)
        log(f"removed the snapshot of {old.name} (older than {KEEP_DAYS} kept days)")
    return facts


def status() -> str:
    if not LAST_OK.exists():
        return "no backup has completed yet"
    f = json.loads(LAST_OK.read_text())
    age = dt.datetime.now() - dt.datetime.fromisoformat(f["at"])
    hours = age.total_seconds() / 3600
    return (f"last good backup {f['at']} ({hours:.0f} h ago{', OVERDUE' if hours > 30 else ''}): {f['messages']} messages, "
            f"transcripts {f['first_day']} to {f['last_day']}, {f['memory_files']} memory files, "
            f"{f['session_files']} session files; {len(complete())} daily snapshots in {SNAPS}")


if __name__ == "__main__":
    if "--status" in sys.argv:
        print(status())
        sys.exit(0)
    try:
        f = backup()
        log(f"ok: {f['messages']} messages (newest {f['newest_message']}), transcripts {f['first_day']} to {f['last_day']}, "
            f"{f['memory_files']} memory files, {f['session_files']} session files -> {f['snapshot']}")
    except Exception as e:                                 # the server asleep or mid-deploy: logged, tried again in six hours
        log(f"FAILED: {e}")
        sys.exit(1)
