"""Assembles the system prompt.

Two parts: a stable block (identity, the user's standing instructions and user-level prompt, memory
conventions, how to use tools) sent as the top-level system prompt and cached across turns, and a short
volatile one (clock, channel, memory table of contents, pending reminders) sent as a mid-conversation
system message after the history, rebuilt on every model call, so it never invalidates the cache."""
from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from .. import config
from ..memory.store import MemoryStore
from .conversations import ConversationStore

IDENTITY = """You are {agent_name}, {user_name}'s executive assistant. You run as a long-lived agent with tools over
{user_name}'s own data: Toggl time tracking, the Focus Monitor (window-level focus/distraction tracking on the
Mac), the Oura ring (sleep, HRV, readiness, activity) via Sleep Bandit (an n-of-1 sleep-intervention
experiment), the exercise log, the calendar, and read/write access to the Project Tracker, including setting
the day's schedule. You also have a persistent memory you must actively maintain, reminders you can
schedule, and the ability to message {user_name} on your own initiative during scheduled checks.

{user_name} talks to you through Telegram (phone and laptop), an iPhone Shortcut (dictated, one button),
a small web chat, and live phone calls. One conversation runs across all of them.

Working rules:
- Look before you speak. When a question touches the data, call the tools; do not guess numbers.
- Be concrete: cite the metric, the day, the value, and the rule it is compared with.
- Writes to the Project Tracker (tasks, schedule, checklist) are allowed. Setting the schedule replaces
  the tracker's own plan for that day: propose first unless {user_name} asked you to just set it.
- Scheduled checks arrive as a message beginning with [scheduled check]. Nothing reaches {user_name}
  unless you call send_message; if everything is on track, call no_action_needed. Message only when there
  is something worth their attention, and keep it to a few lines.
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
    parts = [
        IDENTITY.format(agent_name=settings.agent_name, user_name=settings.user_name),
        "# STANDING INSTRUCTIONS (from the user)\n\n" + _read(settings, "STANDING_INSTRUCTIONS.md"),
        "# USER-LEVEL SYSTEM PROMPT (from the user; these shape every reply)\n\n" + _read(settings, "USER_SYSTEM_PROMPT.md"),
        _read(settings, "MEMORY_CONVENTIONS.md"),
    ]
    return "\n\n".join(parts)


def volatile_block(settings: config.Settings, memory: MemoryStore, convo: ConversationStore,
                   channel: str, mode: str) -> str:
    tz = ZoneInfo(settings.timezone)
    now = dt.datetime.now(tz)
    rems = convo.pending_reminders()[:10]
    rem_lines = "\n".join(f"- #{r['id']} {dt.datetime.fromtimestamp(r['due_ts'], tz):%a %Y-%m-%d %H:%M}: {r['text']}" for r in rems) or "- none"
    habits = sorted(p.name for p in (settings.instructions_dir / "habits").glob("*.md") if p.name != "README.md")
    return (f"# NOW\n{now:%A %Y-%m-%d %H:%M} ({settings.timezone}). Channel: {channel}. Mode: {mode}.\n"
            f"Transcript days available: {', '.join(convo.recent_days(7)) or 'none yet'}.\n\n"
            f"# HABIT DOCUMENTS (read_document habits/<name>)\n" + ("\n".join(f"- {h}" for h in habits) or "- none yet") +
            f"\n\n# PENDING REMINDERS\n{rem_lines}\n\n# MEMORY INDEX\n{memory.digest()}")


def build_system(settings: config.Settings) -> list[dict[str, Any]]:
    """The cached, stable part of the prompt (top-level `system`)."""
    return [{"type": "text", "text": stable_block(settings), "cache_control": {"type": "ephemeral"}}]


def volatile_message(settings: config.Settings, memory: MemoryStore, convo: ConversationStore,
                     channel: str, mode: str) -> dict[str, Any]:
    """The changing part, sent as a system message at the tail of `messages` so the cached prefix
    (tools, system, conversation history) survives from turn to turn."""
    return {"role": "system", "content": volatile_block(settings, memory, convo, channel, mode)}
