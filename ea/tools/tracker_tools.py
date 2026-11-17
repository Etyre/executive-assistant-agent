"""Project Tracker tools, over the tracker's agent API: read the outline, find tasks, add tasks/projects with
attribution, attach signed notes, read the day's scheduling context, set the schedule, steer the day in prose."""
from __future__ import annotations

from typing import Any

from .. import config
from ..connectors.http import SourceError
from ..connectors.http import ServiceRouter
from ..connectors.project_tracker import SCHEDULE_KINDS, ProjectTracker, compact_checklist
from .registry import ToolRegistry

HM = {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$", "description": "HH:MM, 24h local time"}
DATE = {"type": "string", "description": "YYYY-MM-DD; omit for today"}

SCHEDULE_ITEM = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(SCHEDULE_KINDS),
                 "description": "task = a tracker task (needs task_id; title defaults to the task's); routine = a daily-checklist item; break; meeting = calendar."},
        "title": {"type": "string"},
        "start": HM, "end": HM,
        "task_id": {"type": "integer", "description": "Required when kind is task."},
        "note": {"type": "string", "description": "Why this block is here / what to do in it."},
    },
    "required": ["kind", "start", "end"],
    "additionalProperties": False,
}


def register(reg: ToolRegistry, router: ServiceRouter, settings: config.Settings | None = None) -> ProjectTracker:
    tracker = ProjectTracker(router, settings)

    def guard(fn):
        """The app is not running: say so and point at tracker_open instead of a bare connection error."""
        def run(*a, **k):
            try:
                return fn(*a, **k)
            except SourceError as e:
                # Three ways "the app is down" arrives: a refused connection, the bridge's 502 (server deployment),
                # and nightblock's proxy answering 502 for localhost while the night block is on.
                if any(w in str(e) for w in ("Connect", "not answering on the Mac", "could not reach")):
                    return {"error": f"the Project Tracker app is not answering ({str(e)[:120]}); start it with tracker_open, then retry"}
                raise
        run.__name__ = fn.__name__
        return run

    @reg.tool("tracker_open", "Start the Project Tracker app on the Mac if it is not running (its API is unreachable) and wait "
              "for it to answer. Safe when it is already running.", writes=True)
    def tracker_open() -> Any:
        return tracker.launch()

    @reg.tool("tracker_close", "Quit the Project Tracker app on the Mac (the opposite of tracker_open). Its window, menu bar "
              "item and running timer go with it, and your tracker and calendar tools stop working until tracker_open. "
              "Only when the user asks for it.", writes=True)
    def tracker_close() -> Any:
        return tracker.quit()

    @reg.tool("tracker_outline", "The open project/task outline as indented text: '* Project (project ID)' lines and "
              "'- [task ID] title due.. pN BLOCKING ~Nm' lines, with any notes you attached. Use the IDs in other tools.")
    @guard
    def tracker_outline() -> Any:
        return tracker.outline()

    @reg.tool("tracker_find_tasks", "Find open tasks by text (title/description) and/or project id. Returns tasks with their project path.",
              {"properties": {"q": {"type": "string"}, "project_id": {"type": "integer"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}})
    @guard
    def tracker_find_tasks(q: str = "", project_id: int | None = None, limit: int = 50) -> Any:
        return tracker.find_tasks(q, project_id, limit)

    @reg.tool("tracker_task", "Full detail of one task: description, dates, priority, tracked minutes, project path, notes attached.",
              {"properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]})
    @guard
    def tracker_task(task_id: int) -> Any:
        return tracker.task(task_id)

    @reg.tool("tracker_quick_add", "Add tasks from free text; the tracker's Claude files them under the right projects and "
              "parses tags like #p1 #due:fri #blocking. Good default for new tasks. Returns the created task(s) and where they went.",
              {"properties": {"text": {"type": "string"}}, "required": ["text"]}, writes=True)
    @guard
    def tracker_quick_add(text: str) -> Any:
        return tracker.quick_add(text)

    @reg.tool("tracker_create_task", "Create one task with explicit placement and fields. Place it with project_id, or "
              "project_path (names from the outline, e.g. ['Work','Paper']; create_project=true makes a missing path), or "
              "parent_id (sub-task). With none of those the tracker files it itself. `note` attaches your context as a "
              "signed note. Deadline accepts YYYY-MM-DD or words like 'fri', 'tomorrow'.",
              {"properties": {"title": {"type": "string"}, "project_id": {"type": "integer"},
                              "project_path": {"type": "array", "items": {"type": "string"}}, "create_project": {"type": "boolean"},
                              "parent_id": {"type": "integer"}, "description": {"type": "string"}, "deadline": {"type": "string"},
                              "importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1 = low, 5 = critical"},
                              "blocking": {"type": "boolean"}, "blocking_for": {"type": "string"},
                              "estimate_minutes": {"type": "integer"}, "note": {"type": "string"}},
               "required": ["title"]}, writes=True)
    @guard
    def tracker_create_task(title: str, **fields: Any) -> Any:
        return tracker.create_task(title, **fields)

    @reg.tool("tracker_create_project", "Create a project, under parent_id or parent_path (must exist) or at the top level.",
              {"properties": {"name": {"type": "string"}, "parent_id": {"type": "integer"},
                              "parent_path": {"type": "array", "items": {"type": "string"}}, "notes": {"type": "string"}},
               "required": ["name"]}, writes=True)
    @guard
    def tracker_create_project(name: str, parent_id: int | None = None, parent_path: list[str] | None = None, notes: str = "") -> Any:
        return tracker.create_project(name, parent_id, parent_path, notes)

    @reg.tool("tracker_add_note", "Attach context to a task or project as a note signed with your name (never merged into "
              "the user's own text). The tracker's planner also reads task notes. Give exactly one of task_id, project_id.",
              {"properties": {"body": {"type": "string"}, "task_id": {"type": "integer"}, "project_id": {"type": "integer"}},
               "required": ["body"]}, writes=True)
    @guard
    def tracker_add_note(body: str, task_id: int | None = None, project_id: int | None = None) -> Any:
        return tracker.add_note(body, task_id, project_id)

    @reg.tool("tracker_notes", "Notes you have attached: to one task, one project, or all of them.",
              {"properties": {"task_id": {"type": "integer"}, "project_id": {"type": "integer"}}})
    @guard
    def tracker_notes(task_id: int | None = None, project_id: int | None = None) -> Any:
        return tracker.notes(task_id, project_id)

    @reg.tool("tracker_delete_note", "Take back one of your notes.", {"properties": {"note_id": {"type": "integer"}}, "required": ["note_id"]}, writes=True)
    @guard
    def tracker_delete_note(note_id: int) -> Any:
        return tracker.delete_note(note_id)

    @reg.tool("tracker_update_task", "Change a task's title, description, deadline, importance, blocking flag or estimate.",
              {"properties": {"task_id": {"type": "integer"}, "title": {"type": "string"}, "description": {"type": "string"},
                              "deadline": {"type": "string"}, "clear_deadline": {"type": "boolean"},
                              "importance": {"type": "integer", "minimum": 1, "maximum": 5}, "blocking": {"type": "boolean"},
                              "blocking_for": {"type": "string"}, "estimate_minutes": {"type": "integer"}},
               "required": ["task_id"]}, writes=True)
    @guard
    def tracker_update_task(task_id: int, **fields: Any) -> Any:
        return tracker.update_task(task_id, **fields)

    @reg.tool("tracker_set_task_meta", "Set a task's tags and metadata; the tracker records the change with `why` as your "
              "signed note on the task, and the planner uses it. `tags` is the tags as the user types them, in one string: "
              "#p1..#p5 #p0 #due:fri #today #tomorrow #nodue #soft:fri #nosoft #blocking #blocking:Name #notblocking "
              "#onesitting #focus #task #nokind #reading #notreading #writing #notwriting (an unknown tag is refused and "
              "nothing is applied). The same things can be given as named fields, which win over a tag: importance, "
              "deadline (hard; '2026-11-20', 'fri', '' clears), soft_deadline (ideally by), blocking, blocking_for, "
              "chunkable, work_kind ('task', 'focus', '' hands it back), reading, writing. estimate_minutes (0 hands it "
              "back to the forecast) and toggl_title (the timer's name; '' hands it back). Title and description are the "
              "user's text: tracker_update_task changes those.",
              {"properties": {"task_id": {"type": "integer"}, "tags": {"type": "string"}, "why": {"type": "string"},
                              "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                              "deadline": {"type": "string"}, "soft_deadline": {"type": "string"},
                              "blocking": {"type": "boolean"}, "blocking_for": {"type": "string"}, "chunkable": {"type": "boolean"},
                              "work_kind": {"type": "string", "enum": ["task", "focus", ""]},
                              "reading": {"type": "boolean"}, "writing": {"type": "boolean"},
                              "estimate_minutes": {"type": "integer", "minimum": 0, "maximum": 600}, "toggl_title": {"type": "string"}},
               "required": ["task_id"]}, writes=True)
    @guard
    def tracker_set_task_meta(task_id: int, **fields: Any) -> Any:
        return tracker.set_task_meta(task_id, **fields)

    @reg.tool("tracker_set_project_meta", "Set a project's metadata: subtitle (the one line under its name; '' clears it) and "
              "toggl_project (the Toggl project its tasks' timers are filed under; '' unlinks it). The tracker records the "
              "change with `why` as your signed note on the project. Projects carry no tags of their own: tags belong to "
              "tasks (tracker_set_task_meta).",
              {"properties": {"project_id": {"type": "integer"}, "subtitle": {"type": "string"},
                              "toggl_project": {"type": "string"}, "why": {"type": "string"}},
               "required": ["project_id"]}, writes=True)
    @guard
    def tracker_set_project_meta(project_id: int, **fields: Any) -> Any:
        return tracker.set_project_meta(project_id, **fields)

    @reg.tool("tracker_complete_task", "Mark a task done (or undo that).",
              {"properties": {"task_id": {"type": "integer"}, "undo": {"type": "boolean"}}, "required": ["task_id"]}, writes=True)
    @guard
    def tracker_complete_task(task_id: int, undo: bool = False) -> Any:
        return tracker.complete_task(task_id, undo)

    @reg.tool("tracker_schedule_context", "Everything needed to plan a day: the current plan with block statuses and whether "
              "it is pinned, free windows between meetings, prioritised candidate tasks with calibrated estimates and your "
              "notes, routines, today's exercise, the user's scheduling rules document and any standing instructions for the day. "
              "Call this before tracker_set_schedule.",
              {"properties": {"date": DATE}})
    @guard
    def tracker_schedule_context(date: str | None = None) -> Any:
        return tracker.schedule_context(date)

    @reg.tool("tracker_feed", "What the tracker is showing right now: current block, up next, meetings, progress, running timer, "
              "today's exercise, whether the day is off track.")
    @guard
    def tracker_feed() -> Any:
        return tracker.feed()

    @reg.tool("tracker_checklist", "Today's daily checklist (habits), compact: per row its name, steps_to_yellow (N), "
              "markers, today's value, last_true (last date met), misses (consecutive days through yesterday not met) "
              "and colour (green under N misses, yellow at N, orange at 2N, red at 3N). `days` adds the raw values of "
              "the last N dates; `full` returns the whole sheet (every date column; large). Read-only: the user ticks "
              "it himself, never you.",
              {"properties": {"days": {"type": "integer", "minimum": 0, "maximum": 92}, "full": {"type": "boolean"}}})
    @guard
    def tracker_checklist(days: int = 0, full: bool = False) -> Any:
        raw = tracker.checklist()
        if full:
            return raw
        from zoneinfo import ZoneInfo
        import datetime as _dt
        tz = ZoneInfo((settings or config.load()).timezone)
        return compact_checklist(raw, _dt.datetime.now(tz).date(), days=days)

    @reg.tool("tracker_set_schedule", "Set the day's schedule in the Project Tracker block by block. Blocks are clipped to free "
              "time between calendar meetings and anything that did not fit comes back in `dropped`; meetings you leave out "
              "are added from the calendar; blocks already done/active keep their status when you re-send. The day is "
              "pinned: the tracker's own re-planner leaves it alone until tracker_unpin_schedule or a rebuild. Confirm "
              "the plan with the user first unless they asked you to just set it. Include breaks. Task blocks need the "
              "task_id from tracker_schedule_context or tracker_outline.",
              {"properties": {"items": {"type": "array", "items": SCHEDULE_ITEM}, "date": DATE, "work_start": HM, "work_end": HM,
                              "rationale": {"type": "string", "description": "One or two sentences shown in the tracker."},
                              "pin": {"type": "boolean", "description": "default true"}},
               "required": ["items"]}, writes=True)
    @guard
    def tracker_set_schedule(items: list[dict[str, Any]], date: str | None = None, work_start: str | None = None,
                             work_end: str | None = None, rationale: str = "", pin: bool = True) -> Any:
        return tracker.set_schedule(items, date, work_start, work_end, rationale, pin)

    @reg.tool("tracker_set_day_instructions", "Steer a day's plan in prose instead of laying out blocks ('end by 13:00', "
              "'grant report first thing', 'dentist at 14:00, keep the afternoon light'). The tracker re-plans the day with "
              "these appended to the user's rules (that unpins the day) and shows them on the plan page.",
              {"properties": {"text": {"type": "string"}, "date": DATE, "rebuild": {"type": "boolean", "description": "default true"}},
               "required": ["text"]}, writes=True)
    @guard
    def tracker_set_day_instructions(text: str, date: str | None = None, rebuild: bool = True) -> Any:
        return tracker.set_day_instructions(text, date, rebuild)

    @reg.tool("tracker_clear_day_instructions", "Drop the standing instructions for a day.", {"properties": {"date": DATE}}, writes=True)
    @guard
    def tracker_clear_day_instructions(date: str | None = None) -> Any:
        return tracker.clear_day_instructions(date)

    @reg.tool("tracker_rebuild_schedule", "Let the tracker plan the day itself (its own Claude scheduler), optionally from now "
              "and/or extending the work day until HH:MM. This replaces and unpins a schedule you set.",
              {"properties": {"from_now": {"type": "boolean"}, "until": HM, "date": DATE}}, writes=True)
    @guard
    def tracker_rebuild_schedule(from_now: bool = True, until: str | None = None, date: str | None = None) -> Any:
        return tracker.rebuild_plan(from_now, until, date)

    @reg.tool("tracker_unpin_schedule", "Hand a pinned day back to the tracker's automatic re-planner.", {"properties": {"date": DATE}}, writes=True)
    @guard
    def tracker_unpin_schedule(date: str | None = None) -> Any:
        return tracker.unpin_plan(date)

    return tracker
