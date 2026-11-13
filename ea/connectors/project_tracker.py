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
import pathlib
import subprocess
import time
from typing import Any

import httpx

from .. import config
from .http import ServiceRouter, SourceError, get_json, send_json

SCHEDULE_KINDS = ("task", "routine", "exercise", "journal", "break", "meeting", "checklist")


class ProjectTracker:
    def __init__(self, router: ServiceRouter, settings: config.Settings | None = None):
        self.router = router
        s = settings or config.load()
        self.headers = {"X-Agent-Name": s.agent_name}
        self.app_path = s.sources.tracker_app
        tok = config.secret(s.sources.tracker_agent_token_env)
        if tok:
            self.headers["Authorization"] = f"Bearer {tok}"

    def _c(self) -> httpx.Client:
        c = self.router.client("tracker")
        c.headers.update(self.headers)
        return c

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
        """Start the Project Tracker app when its API is not answering. Only possible on the Mac itself (through
        the bridge the app is on another machine). `open -g` so the window does not take the user's focus."""
        if self.running():
            return {"ok": True, "status": "already running"}
        if self.router.bridge:
            return {"ok": False, "error": "the tracker is reached through the Mac bridge; it can only be started on the Mac itself"}
        app = pathlib.Path(self.app_path).expanduser()
        if not app.exists():
            return {"ok": False, "error": f"tracker app not found at {app}; set sources.tracker_app"}
        subprocess.run(["open", "-g", str(app)], check=True, timeout=15)
        t0 = time.time()
        while time.time() - t0 < wait_seconds:
            if self.running():
                return {"ok": True, "status": "started", "seconds": round(time.time() - t0, 1)}
            time.sleep(1)
        return {"ok": False, "error": f"opened {app.name} but its API did not answer within {wait_seconds:.0f}s; see launcher.log in its data folder"}

    def tree(self) -> dict[str, Any]:
        """{projects: [{id, parent_id, name, path, notes, source, open_tasks, notes_added}], tree: nested}"""
        with self._c() as c:
            return get_json(c, "/api/agent/tree")

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
        with self._c() as c:
            return get_json(c, "/api/agent/tasks", q=q or None, project_id=project_id, limit=limit)

    def task(self, tid: int) -> dict[str, Any]:
        with self._c() as c:
            return get_json(c, f"/api/agent/tasks/{tid}")

    def schedule_context(self, date: str | None = None) -> dict[str, Any]:
        """The day as the tracker sees it: current plan with statuses, free windows between meetings, prioritised
        candidate tasks with calibrated estimates, routines, exercise, rules document, standing instructions."""
        with self._c() as c:
            return get_json(c, "/api/agent/schedule", date=date)

    def feed(self) -> dict[str, Any]:
        with self._c() as c:
            return get_json(c, "/api/feed")

    def calendar(self, date: str | None = None) -> list[dict[str, Any]]:
        with self._c() as c:
            return get_json(c, "/api/calendar", date=date)

    def checklist(self) -> dict[str, Any]:
        with self._c() as c:
            return get_json(c, "/api/checklist")


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

    def notes(self, task_id: int | None = None, project_id: int | None = None) -> list[dict[str, Any]]:
        with self._c() as c:
            return get_json(c, "/api/agent/notes", task_id=task_id, project_id=project_id)

    # ---------------------------------------------------------------- write (agent API)
    def create_task(self, title: str, **fields: Any) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "POST", "/api/agent/tasks", {"title": title, **{k: v for k, v in fields.items() if v is not None}})

    def create_project(self, name: str, parent_id: int | None = None, parent_path: list[str] | None = None, notes: str = "") -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "POST", "/api/agent/projects", {"name": name, "parent_id": parent_id, "parent_path": parent_path, "notes": notes})

    def quick_add(self, text: str) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "POST", "/api/agent/quick_add", {"text": text})

    def add_note(self, body: str, task_id: int | None = None, project_id: int | None = None) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "POST", "/api/agent/notes", {"body": body, "task_id": task_id, "project_id": project_id})

    def delete_note(self, nid: int) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "DELETE", f"/api/agent/notes/{nid}")

    def set_schedule(self, items: list[dict[str, Any]], date: str | None = None, work_start: str | None = None,
                     work_end: str | None = None, rationale: str = "", pin: bool = True) -> dict[str, Any]:
        """Returns {ok, dropped: [blocks that did not fit], plan}."""
        body = {"items": items, "date": date, "work_start": work_start, "work_end": work_end, "rationale": rationale, "pin": pin}
        with self._c() as c:
            return send_json(c, "PUT", "/api/agent/schedule", body)

    def set_day_instructions(self, text: str, date: str | None = None, rebuild: bool = True) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "PUT", "/api/agent/schedule/instructions", {"text": text, "date": date, "rebuild": rebuild})

    def clear_day_instructions(self, date: str | None = None) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "DELETE", "/api/agent/schedule/instructions", None, date=date)

    def rebuild_plan(self, from_now: bool = True, until: str | None = None, date: str | None = None) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "POST", "/api/agent/schedule/rebuild", {"from_now": from_now, "until": until, "date": date})

    def unpin_plan(self, date: str | None = None) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "POST", "/api/agent/schedule/unpin", None, date=date)

    # ---------------------------------------------------------------- write (plain API; not covered by the agent API)
    def update_task(self, tid: int, **fields: Any) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "PATCH", f"/api/tasks/{tid}", {k: v for k, v in fields.items() if v is not None})

    def complete_task(self, tid: int, undo: bool = False) -> dict[str, Any]:
        with self._c() as c:
            return send_json(c, "POST", f"/api/tasks/{tid}/{'uncomplete' if undo else 'complete'}")
