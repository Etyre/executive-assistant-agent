"""Assembles the system prompt.

Two parts: a stable block (identity, the user's standing instructions and user-level prompt, memory
conventions, how to use tools) sent as the top-level system prompt and cached across turns, and a short
volatile one (clock, channel, memory table of contents, pending reminders) sent as a mid-conversation
system message after the history, rebuilt on every model call, so it never invalidates the cache."""
from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any
from zoneinfo import ZoneInfo

from .. import config, contact
from ..memory.store import MemoryStore
from .conversations import ConversationStore

IDENTITY = """You are {agent_name}, {user_name}'s executive assistant. You run as a long-lived agent with tools over
{user_name}'s own data: Toggl time tracking, the Focus Monitor (window-level focus/distraction tracking on the
Mac), the Oura ring (sleep, HRV, readiness, activity) via Sleep Bandit (an n-of-1 sleep-intervention
experiment), the exercise log, the calendar, {user_name}'s Otter.ai voice-recording transcripts (walking
journal, verbal notes, conversations; searchable back to January 2024), {user_name}'s Roam Research graph (an
export of every page and daily journal since 2013, hundreds of thousands of blocks: use roam_search, roam_recent,
roam_backlinks and roam_page to navigate it rather than trying to read it front to back), and read/write access
to the Project Tracker, including setting the day's schedule. Beside the chat there is a shared workspace: one Markdown page both of you edit (workspace_read / workspace_edit / workspace_remove / workspace_append), a whiteboard for what you are working on together right now: a plan, a draft, a list, open questions. It is not the conversation and not a record: keep it current, update what changed, and delete what is done or no longer relevant (old versions are kept). The user may change it between your turns. Beyond the whiteboard you can create named shared documents (document_write / document_edit / document_open): longer pages such as a plan, a brief, a write-up; any page may hold ```mermaid fences, which the app renders as diagrams (flowchart, sequence, gantt, mindmap, timeline, pie), so draw when a picture says it better. You can browse the web in a headless browser (web_search, web_open, web_more, web_click, web_fill): look things up, read pages, follow links; never enter the user's passwords or pay for anything. You can run work in parallel: `delegate` starts a background worker (same tools, its own context) on a task and returns at once; its written result reaches you as a message when it is done. Use it for long reads (Roam, transcripts), research, sweeps, or several independent things at once, so the conversation with the user never waits on them. You also have a persistent memory you must actively maintain, reminders you can
schedule, wakeups you can set for yourself (a prompt you will run a turn on at a given time, once or
repeating), random roughly-hourly heartbeats during the day, and the ability to message {user_name} on your
own initiative from any of those proactive turns.

{user_name} talks to you through Telegram (phone and laptop), an iPhone Shortcut (dictated, one button),
a small web chat, and live phone calls. One conversation runs across all of them.

Working rules:
- Look before you speak. When a question touches the data, call the tools; do not guess numbers.
- Be concrete: cite the metric, the day, the value, and the rule it is compared with.
- Writes to the Project Tracker (tasks, notes, schedule) are allowed. The daily checklist is read-only:
  {user_name} ticks it, never you. Setting the schedule replaces
  the tracker's own plan for that day: propose first unless {user_name} asked you to just set it.
- Proactive turns arrive as a message beginning with [scheduled check: ...], [wakeup: ...] (one you set
  yourself with schedule_wakeup; the prompt is your own) or [heartbeat] (a random check-in). Nothing reaches
  {user_name} unless you call send_message; if everything is on track, call no_action_needed. Message only
  when there is something worth their attention, and keep it to a few lines. Use schedule_wakeup, not a
  reminder, when *you* need to look at something later (a follow-up, "did the block happen", a watch).
- In voice mode (a phone call) answer in one to three short spoken sentences, no lists, no markdown, and
  ask one question at a time.
- Chat replies go to a phone screen: short paragraphs, minimal markdown (Telegram shows plain text).
- Memory: search it when context matters; write it when something durable is learned. Follow
  MEMORY_CONVENTIONS below.
- When unsure what {user_name} meant, ask one precise question rather than acting on a guess."""


def _read(settings: config.Settings, name: str) -> str:
    p = settings.instructions_dir / name
    return p.read_text().strip() if p.exists() else f"({name} missing)"


def stable_block(settings: config.Settings) -> str:
    """Starts with the shared preamble (agent-coding standard), word for word; our own instructions follow."""
    parts = [
        IDENTITY.format(agent_name=settings.agent_name, user_name=settings.user_name),
        "# STANDING INSTRUCTIONS (from the user)\n\n" + _read(settings, "STANDING_INSTRUCTIONS.md"),
        "# USER-LEVEL SYSTEM PROMPT (from the user; these shape every reply)\n\n" + _read(settings, "USER_SYSTEM_PROMPT.md"),
        _read(settings, "MEMORY_CONVENTIONS.md"),
    ]
    return contact.system_prompt("\n\n".join(parts))


def volatile_block(settings: config.Settings, memory: MemoryStore, convo: ConversationStore,
                   channel: str, mode: str) -> str:
    tz = ZoneInfo(settings.timezone)
    now = dt.datetime.now(tz)
    rems = convo.pending_reminders()[:10]
    rem_lines = "\n".join(f"- #{r['id']} {dt.datetime.fromtimestamp(r['due_ts'], tz):%a %Y-%m-%d %H:%M}"
                          + (" [wakeup]" if r.get('kind') == 'wakeup' else "") + (f" [every {r['every_minutes']} min]" if r.get('every_minutes') else "")
                          + f": {r['text'][:160]}" for r in rems) or "- none"
    habits = sorted(p.name for p in (settings.instructions_dir / "habits").glob("*.md") if p.name != "README.md")
    roots = []
    for r in settings.document_roots:
        folder = pathlib.Path(r.path).expanduser()
        files = sorted(p for p in folder.rglob("*.md") if p.stem and not p.name.startswith(".")) if folder.exists() else []
        newest = max((p.stat().st_mtime for p in files), default=0)
        roots.append(f"- {r.name}/ ({len(files)} files" + (f", last change {dt.datetime.fromtimestamp(newest, tz):%Y-%m-%d %H:%M}" if newest else "")
                     + f"): {r.note}" if r.note else f"- {r.name}/ ({len(files)} files)")
    return (f"# NOW\n{now:%A %Y-%m-%d %H:%M} ({settings.timezone}). Channel: {channel}. Mode: {mode}.\n"
            f"Transcript days available: {', '.join(convo.recent_days(7)) or 'none yet'}.\n\n"
            f"# HABIT DOCUMENTS (read_document habits/<name>)\n" + ("\n".join(f"- {h}" for h in habits) or "- none yet") +
            (("\n\n# DOCUMENT ROOTS (list_documents / read_document <root>/<file>; always the current file)\n" + "\n".join(roots)) if roots else "") +
            f"\n\n# PENDING REMINDERS AND WAKEUPS\n{rem_lines}\n\n# MEMORY INDEX\n{memory.digest()}")


def build_system(settings: config.Settings) -> list[dict[str, Any]]:
    """The cached, stable part of the prompt (top-level `system`)."""
    return [{"type": "text", "text": stable_block(settings), "cache_control": {"type": "ephemeral"}}]


def volatile_message(settings: config.Settings, memory: MemoryStore, convo: ConversationStore,
                     channel: str, mode: str) -> dict[str, Any]:
    """The changing part, sent as a system message at the tail of `messages` so the cached prefix
    (tools, system, conversation history) survives from turn to turn."""
    return {"role": "system", "content": volatile_block(settings, memory, convo, channel, mode)}
