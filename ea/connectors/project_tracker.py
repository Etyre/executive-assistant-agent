"""Project Tracker (local FastAPI on :8791).

Everything the agent adds or changes goes through the tracker's agent API (`/api/agent/*`): tasks and
projects it creates are attributed to it, context it attaches is a separate signed note, and a schedule it
sets is pinned against the tracker's own re-planner. The agent names itself with `X-Agent-Name` and sends
the optional bearer token (`AGENT_API_TOKEN`, Settings -> Assistant agent in the tracker). A few plain
read/write calls that the agent API does not cover (feed, completing a task) use the app's ordinary API,
which has no auth and is only reachable on localhost or through the bridge. The daily checklist is read-only
for the agent: the user ticks it, so there is deliberately no write path to it here."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import threading
import time
from typing import Any

import httpx

from .. import config
from .http import MAC_AWAY, ServiceRouter, SourceError, get_json, send_json

SCHEDULE_KINDS = ("task", "routine", "exercise", "journal", "break", "meeting", "checklist")


def app_pids(app: pathlib.Path) -> list[int]:
    """The processes running out of an app bundle, the one with the shortest command line first: for the tracker
    that is its launcher (no arguments), which must stop before the Python it would otherwise restart."""
    out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    found = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if cmd.strip().startswith(str(app) + "/Contents/MacOS/"):
            found.append((len(cmd.strip()), int(pid)))
    return [pid for _, pid in sorted(found)]


# One lock for every read-modify-write of the outbox file, across every ProjectTracker in the process (the tools
# and the scheduler each have one).
_OUTBOX = threading.Lock()

BESIDE = ("The Project Tracker's server runs on the server beside you, not as an app on Eli's Mac, so there is nothing "
          "for you to open or close. If it is not answering, tell Eli: `deploy/tailnet/server.sh status` in the tracker's project.")


class ProjectTracker:
    def __init__(self, router: ServiceRouter, settings: config.Settings | None = None):
        self.router = router
        s = settings or config.load()
        self.headers = {"X-Agent-Name": s.agent_name}
        self.app_path = s.sources.tracker_app
        self.copy_url = s.sources.tracker_copy_url.rstrip("/") if router.bridge else ""
        self.outbox_path = s.data_dir / "tracker_outbox.jsonl"
        self.failed_path = s.data_dir / "tracker_outbox_failed.jsonl"
        self._notes: list[str] = []       # not `notes`: that name is the method below, and shadowing it broke tracker_notes (2026-11-18)
        tok = config.secret(s.sources.tracker_agent_token_env)
        if tok:
            self.headers["Authorization"] = f"Bearer {tok}"

    def _c(self) -> httpx.Client:
        c = self.router.client("tracker")
        c.headers.update(self.headers)
        return c

    # ---------------------------------------------------------------- the Mac asleep (server deployment only)
    # The tracker is on Eli's Mac. While the Mac is awake every call goes there. While it is asleep, reads are
    # answered by the read-only copy on the server (sources.tracker_copy_url; synced from the Mac every five minutes)
    # and changes are kept in an outbox and made on the Mac when it wakes (replay, from the scheduler).
    def _away(self, e: SourceError) -> bool:
        return bool(self.copy_url) and MAC_AWAY[:30] in str(e)

    def _get(self, path: str, **params: Any) -> Any:
        try:
            with self._c() as c:
                return get_json(c, path, **params)
        except SourceError as e:
            if not self._away(e):
                raise
        with httpx.Client(base_url=self.copy_url, timeout=30, headers=self.headers) as c:
            out = get_json(c, path, **params)
            at = (get_json(c, "/api/replica") or {}).get("synced_at")
        when = dt.datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M") if at else "an unknown time"
        waiting = len(self._outbox())
        self._notes = [n for n in self._notes if not n.startswith("Eli's Mac is asleep")]     # one such note per result
        self._notes.append(f"Eli's Mac is asleep, so this is the server's copy of the tracker as of {when} (server time)."
                          + (f" {waiting} change(s) you made since are waiting for the Mac and are not in it." if waiting else ""))
        return out

    def _send(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        try:
            with self._c() as c:
                return send_json(c, method, path, body, **params)
        except SourceError as e:
            if not self._away(e):
                raise
        rec = {"at": time.time(), "id": os.urandom(6).hex(), "method": method, "path": path, "body": body,
               "params": {k: v for k, v in params.items() if v is not None}}
        with _OUTBOX:
            with open(self.outbox_path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"queued": True, "note": "Eli's Mac is asleep, so the tracker could not be changed just now. The change is saved "
                "and will be made when the Mac wakes; nothing more is needed from you. Until then the tracker's copy will "
                "not show it, and a new task or project has no ID yet."}

    def _outbox(self) -> list[dict[str, Any]]:
        try:
            return [json.loads(ln) for ln in self.outbox_path.read_text().splitlines() if ln.strip()]
        except OSError:
            return []

    def _settled(self, rec: dict[str, Any]) -> int:
        """Take one change out of the outbox file; returns how many are left. The file is read again under the
        lock, never written back from a list read earlier: replay used to do that, and a change queued while a
        replay was waiting on the sleeping Mac was wiped out (one of two tasks created together, 2026-11-18)."""
        with _OUTBOX:
            left = self._outbox()
            for i, r in enumerate(left):
                if r == rec:
                    del left[i]
                    break
            tmp = self.outbox_path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in left))
            tmp.replace(self.outbox_path)
            return len(left)

    def replay(self) -> dict[str, int]:
        """Make the waiting changes on the Mac, oldest first. Stops at the first one that finds the Mac away again;
        one the tracker refuses is dropped and remembered (failed_path) so the next tracker tool result says so.
        While the Mac is away the file is not touched at all."""
        waiting, done, failed = self._outbox(), 0, 0
        left = len(waiting)
        for rec in waiting:
            try:
                with self._c() as c:
                    send_json(c, rec["method"], rec["path"], rec.get("body"), **rec.get("params", {}))
                done += 1
            except SourceError as e:
                if MAC_AWAY[:30] in str(e):
                    break
                failed += 1
                with open(self.failed_path, "a") as f:
                    f.write(json.dumps({**rec, "error": str(e)[:300]}, ensure_ascii=False) + "\n")
            left = self._settled(rec)
        return {"done": done, "failed": failed, "waiting": left}

    def take_notes(self) -> list[str]:
        """What a tool result should carry besides its data: "this is the copy", and changes that failed on replay."""
        notes, self._notes = self._notes, []
        try:
            failed = [json.loads(ln) for ln in self.failed_path.read_text().splitlines() if ln.strip()]
            self.failed_path.unlink()
        except OSError:
            failed = []
        for f in failed:
            when = dt.datetime.fromtimestamp(f["at"]).strftime("%Y-%m-%d %H:%M")
            notes.append(f"A change you made at {when} while the Mac was asleep was refused when it was applied: "
                         f"{f['method']} {f['path']} {json.dumps(f.get('body'), ensure_ascii=False)[:200]} -> {f['error']}")
        return notes

    # ---------------------------------------------------------------- read
    def ping(self) -> dict[str, Any]:
        with self._c() as c:
            return get_json(c, "/api/agent")

    def running(self) -> bool:
        try:
            self.ping()
            return True
        except SourceError:
            return False

    def launch(self, wait_seconds: float = 20.0) -> dict[str, Any]:
        """Start the Project Tracker app when its API is not answering (on a server, the bridge on the Mac does
        the opening). `open -g` so the window does not take the user's focus."""
        if self.running():
            return {"ok": True, "status": "already running"}
        if self._beside():
            return {"ok": False, "error": BESIDE}
        if self.router.via_bridge("tracker"):                 # on a server: ask the bridge on the Mac to open it
            try:
                self.router.mac("POST", "/mac/open-tracker")
            except SourceError as e:
                return {"ok": False, "error": str(e)}
            return self._wait_running(wait_seconds, "the Project Tracker")
        app = pathlib.Path(self.app_path).expanduser()
        if not app.exists():
            return {"ok": False, "error": f"tracker app not found at {app}; set sources.tracker_app"}
        subprocess.run(["open", "-g", str(app)], check=True, timeout=15)
        return self._wait_running(wait_seconds, app.name)

    def quit(self, wait_seconds: float = 15.0) -> dict[str, Any]:
        """Quit the Project Tracker app (the opposite of launch). Its API, menu bar item and timer go with it. The
        app is a native launcher plus the Python it runs, and the launcher restarts Python when Python alone dies,
        so both are stopped, launcher first (the tracker's README, "quit both")."""
        if self._beside():
            return {"ok": False, "error": BESIDE}
        if not self.running():
            return {"ok": True, "status": "already closed"}
        if self.router.via_bridge("tracker"):
            try:
                self.router.mac("POST", "/mac/close-tracker")
            except SourceError as e:
                return {"ok": False, "error": str(e)}
        else:
            pids = app_pids(pathlib.Path(self.app_path).expanduser())
            if not pids:
                return {"ok": False, "error": "the tracker's API answers but it was not started from its app; quit it from its menu bar item"}
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        t0 = time.time()
        while time.time() - t0 < wait_seconds:
            if not self.running():
                return {"ok": True, "status": "closed", "seconds": round(time.time() - t0, 1)}
            time.sleep(1)
        return {"ok": False, "error": f"asked the Project Tracker to quit but its API still answers after {wait_seconds:.0f}s"}

    def _beside(self) -> bool:
        """The tracker's server runs on this machine's side (sources.direct), not as an app on the Mac."""
        return bool(self.router.bridge) and not self.router.via_bridge("tracker")

    def _wait_running(self, wait_seconds: float, name: str) -> dict[str, Any]:
        t0 = time.time()
        while time.time() - t0 < wait_seconds:
            if self.running():
                return {"ok": True, "status": "started", "seconds": round(time.time() - t0, 1)}
            time.sleep(1)
        return {"ok": False, "error": f"opened {name} but its API did not answer within {wait_seconds:.0f}s; see launcher.log in its data folder"}

    def tree(self) -> dict[str, Any]:
        """{projects: [{id, parent_id, name, path, notes, source, open_tasks, notes_added}], tree: nested}"""
        return self._get("/api/agent/tree")

    def outline(self, max_tasks_per_project: int = 40) -> str:
        """The open outline as indented text, which models read far better than nested JSON."""
        lines: list[str] = []

        def task_line(t: dict[str, Any], depth: int) -> None:
            bits = [f"[{t['id']}]", t.get("title", "")]
            if t.get("deadline"):
                bits.append(f"due {t['deadline']}")
            if t.get("importance"):
                bits.append(f"p{t['importance']}")
            if t.get("blocking"):
                bits.append("BLOCKING" + (f" {t['blocking_for']}" if t.get("blocking_for") else ""))
            if t.get("estimate_minutes"):
                bits.append(f"~{t['estimate_minutes']}m")
            if t.get("source") == "agent":
                bits.append("(added by agent)")
            lines.append("  " * depth + "- " + " ".join(str(b) for b in bits))
            for n in t.get("notes_added") or []:
                lines.append("  " * (depth + 1) + f"note [{n.get('author')}]: {n.get('body')}")
            for ch in t.get("children") or []:
                task_line(ch, depth + 1)

        def walk(nodes: list[dict[str, Any]], depth: int) -> None:
            for p in nodes:
                lines.append("  " * depth + f"* {p['name']} (project {p['id']})")
                for n in p.get("notes_added") or []:
                    lines.append("  " * (depth + 1) + f"note [{n.get('author')}]: {n.get('body')}")
                tasks = p.get("tasks") or []
                for t in tasks[:max_tasks_per_project]:
                    task_line(t, depth + 1)
                if len(tasks) > max_tasks_per_project:
                    lines.append("  " * (depth + 1) + f"- … {len(tasks) - max_tasks_per_project} more tasks")
                walk(p.get("children") or [], depth + 1)

        walk(self.tree().get("tree", []), 0)
        return "\n".join(lines) or "(no projects)"

    def find_tasks(self, q: str = "", project_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._get("/api/agent/tasks", q=q or None, project_id=project_id, limit=limit)

    def task(self, tid: int) -> dict[str, Any]:
        return self._get(f"/api/agent/tasks/{tid}")

    def schedule_context(self, date: str | None = None) -> dict[str, Any]:
        """The day as the tracker sees it: current plan with statuses, free windows between meetings, prioritised
        candidate tasks with calibrated estimates, routines, exercise, rules document, standing instructions."""
        return self._get("/api/agent/schedule", date=date)

    def feed(self) -> dict[str, Any]:
        return self._get("/api/feed")

    def calendar(self, date: str | None = None) -> list[dict[str, Any]]:
        return self._get("/api/calendar", date=date)

    def checklist(self) -> dict[str, Any]:
        return self._get("/api/checklist")


    def notes(self, task_id: int | None = None, project_id: int | None = None) -> list[dict[str, Any]]:
        return self._get("/api/agent/notes", task_id=task_id, project_id=project_id)

    # ---------------------------------------------------------------- write (agent API)
    def create_task(self, title: str, **fields: Any) -> dict[str, Any]:
        return self._send("POST", "/api/agent/tasks", {"title": title, **{k: v for k, v in fields.items() if v is not None}})

    def create_project(self, name: str, parent_id: int | None = None, parent_path: list[str] | None = None, notes: str = "") -> dict[str, Any]:
        return self._send("POST", "/api/agent/projects", {"name": name, "parent_id": parent_id, "parent_path": parent_path, "notes": notes})

    def quick_add(self, text: str) -> dict[str, Any]:
        return self._send("POST", "/api/agent/quick_add", {"text": text})

    def add_note(self, body: str, task_id: int | None = None, project_id: int | None = None) -> dict[str, Any]:
        return self._send("POST", "/api/agent/notes", {"body": body, "task_id": task_id, "project_id": project_id})

    def delete_note(self, nid: int) -> dict[str, Any]:
        return self._send("DELETE", f"/api/agent/notes/{nid}")

    def set_schedule(self, items: list[dict[str, Any]], date: str | None = None, work_start: str | None = None,
                     work_end: str | None = None, rationale: str = "", pin: bool = True) -> dict[str, Any]:
        """Returns {ok, dropped: [blocks that did not fit], plan}."""
        body = {"items": items, "date": date, "work_start": work_start, "work_end": work_end, "rationale": rationale, "pin": pin}
        return self._send("PUT", "/api/agent/schedule", body)

    def set_day_instructions(self, text: str, date: str | None = None, rebuild: bool = True) -> dict[str, Any]:
        return self._send("PUT", "/api/agent/schedule/instructions", {"text": text, "date": date, "rebuild": rebuild})

    def clear_day_instructions(self, date: str | None = None) -> dict[str, Any]:
        return self._send("DELETE", "/api/agent/schedule/instructions", None, date=date)

    def rebuild_plan(self, from_now: bool = True, until: str | None = None, date: str | None = None) -> dict[str, Any]:
        return self._send("POST", "/api/agent/schedule/rebuild", {"from_now": from_now, "until": until, "date": date})

    def unpin_plan(self, date: str | None = None) -> dict[str, Any]:
        return self._send("POST", "/api/agent/schedule/unpin", None, date=date)

    def set_task_meta(self, tid: int, **fields: Any) -> dict[str, Any]:
        """Tags, priority, dates, estimate, timer name. "" and 0 are real values here (they clear), only None is left out."""
        return self._send("PATCH", f"/api/agent/tasks/{tid}", {k: v for k, v in fields.items() if v is not None})

    def set_project_meta(self, pid: int, **fields: Any) -> dict[str, Any]:
        return self._send("PATCH", f"/api/agent/projects/{pid}", {k: v for k, v in fields.items() if v is not None})

    # ---------------------------------------------------------------- write (plain API; not covered by the agent API)
    def update_task(self, tid: int, **fields: Any) -> dict[str, Any]:
        return self._send("PATCH", f"/api/tasks/{tid}", {k: v for k, v in fields.items() if v is not None})

    def complete_task(self, tid: int, undo: bool = False) -> dict[str, Any]:
        return self._send("POST", f"/api/tasks/{tid}/{'uncomplete' if undo else 'complete'}")

    def move_task(self, tid: int, project_id: int | None = None, parent_id: int | None = None, after_id: int | None = None,
                  top_level: bool = False) -> dict[str, Any]:
        """Under another task (parent_id; the project follows the parent), into a project (project_id; top level
        there), or out to the top level of its own project (top_level). after_id: right after that sibling."""
        body: dict[str, Any] = {}
        if project_id is not None:
            body["project_id"] = project_id
        if parent_id is not None or top_level:
            body["parent_id"] = None if top_level else parent_id      # the tracker applies an explicit null
        if after_id is not None:
            body["after_id"] = after_id
        return self._send("PATCH", f"/api/tasks/{tid}", body)

    def archive_task(self, tid: int, restore: bool = False) -> dict[str, Any]:
        """Put a task and everything under it away, finished or not (the tracker's "delete": nothing is lost)."""
        return self._send("POST", f"/api/tasks/{tid}/{'restore' if restore else 'archive'}")

    def archive_project(self, pid: int, restore: bool = False) -> dict[str, Any]:
        if restore:
            return self._send("POST", f"/api/projects/{pid}/restore")
        return self._send("DELETE", f"/api/projects/{pid}")           # the tracker's DELETE archives, with sub-projects

    def delete_empty_project(self, pid: int) -> dict[str, Any]:
        """For good; the tracker refuses unless the project never had a task and has no sub-projects."""
        return self._send("POST", f"/api/projects/{pid}/delete_empty")


MARKERS = {"Paused at workshops": "paused_at_workshops", "Covered when overwelmed": "covered_when_overwhelmed", "dimmed": "dimmed"}


def compact_checklist(raw: dict[str, Any], today: dt.date, days: int = 0) -> dict[str, Any]:
    """The checklist sheet boiled down for the model: the full sheet carries every date column of the quarter
    (about 50 KB, more than the Agent SDK passes back as a tool result; Atlas, 2026-11-12). Per row: the name,
    its colour rule (steps_to_yellow = N: yellow at N consecutive misses, orange at 2N, red at 3N), the markers,
    today's value, the last date it was met, the current run of misses through yesterday, and the colour. A miss
    is a day whose value is not the row's "Value for green" (true unless the sheet says otherwise). `days` adds
    the raw values of the last `days` dates."""
    out = []
    for it in raw.get("items", []):
        f = it.get("fields") or {}
        green = f.get("Value for green", True)
        dated = sorted((k, v) for k, v in f.items() if len(k) == 10 and k[:4].isdigit())
        past = [(k, v) for k, v in dated if k < today.isoformat()]
        row: dict[str, Any] = {"item": it.get("item"), "checkable": it.get("checkable"), "computed": it.get("computed")}
        n = f.get("steps to Yellow")
        if isinstance(n, int) and n > 0:
            row["steps_to_yellow"] = n
        if green is not True:
            row["value_for_green"] = green
        markers = [name for col, name in MARKERS.items() if f.get(col) not in (None, "", False, 0)]
        if markers:
            row["markers"] = markers
        row["today"] = f.get(today.isoformat()) if it.get("checkable") else None
        hits = [k for k, v in dated if v == green and k <= today.isoformat()]
        row["last_true"] = hits[-1] if hits else None
        misses = 0
        for k, v in reversed(past):
            if v == green:
                break
            misses += 1
        row["misses"] = misses
        if "steps_to_yellow" in row:
            row["colour"] = "red" if misses >= 3 * n else "orange" if misses >= 2 * n else "yellow" if misses >= n else "green"
        if days > 0:
            row["recent"] = {k: v for k, v in dated if k <= today.isoformat()}
            row["recent"] = dict(list(row["recent"].items())[-days:])
        out.append(row)
    return {"source": raw.get("source"), "today": today.isoformat(), "rows": out,
            "legend": "misses = consecutive days through yesterday not meeting the row; colour from steps_to_yellow N: "
                      "yellow at N, orange at 2N, red at 3N"}
