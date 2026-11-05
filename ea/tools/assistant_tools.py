"""The assistant's own actions: reminders, proactive messages, instruction/habit documents, transcripts,
and ending a conversation (the user's standing instructions give the agent that right)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import config
from ..agent.conversations import ConversationStore
from ..channels import twilio_voice
from .registry import ToolRegistry


def register(reg: ToolRegistry, store: ConversationStore, settings: config.Settings,
             send_message: Callable[[str], None], end_conversation: Callable[[str], None]) -> None:
    tz = ZoneInfo(settings.timezone)
    inst_dir = settings.instructions_dir

    @reg.tool("set_reminder", "Send the user a message at a given local time (delivered on the chat channel). Use for "
              "'remind me at...', follow-ups you promised, and checks you want to make later.",
              {"properties": {"when": {"type": "string", "description": "ISO local datetime, e.g. 2026-11-05T09:30"},
                              "text": {"type": "string", "description": "The message to send, written to the user."}},
               "required": ["when", "text"]}, writes=True)
    def set_reminder(when: str, text: str) -> Any:
        due = dt.datetime.fromisoformat(when)
        if due.tzinfo is None:
            due = due.replace(tzinfo=tz)
        rid = store.add_reminder(due, text)
        return {"reminder_id": rid, "due": due.isoformat(timespec="minutes")}

    @reg.tool("list_reminders", "Reminders not yet sent.")
    def list_reminders() -> Any:
        return [{"id": r["id"], "due": dt.datetime.fromtimestamp(r["due_ts"], tz).isoformat(timespec="minutes"), "text": r["text"]}
                for r in store.pending_reminders()]

    @reg.tool("cancel_reminder", "Cancel a pending reminder.", {"properties": {"reminder_id": {"type": "integer"}}, "required": ["reminder_id"]}, writes=True)
    def cancel_reminder(reminder_id: int) -> Any:
        return "cancelled" if store.cancel_reminder(reminder_id) else "no such pending reminder"

    @reg.tool("send_message", "Send the user a message now on the chat channel. In a normal chat turn your reply is already "
              "delivered, so this is for scheduled checks (where nothing is sent unless you call it) and for a second, "
              "separate message.",
              {"properties": {"text": {"type": "string"}}, "required": ["text"]}, writes=True)
    def send_message_tool(text: str) -> Any:
        send_message(text)
        return "sent"

    @reg.tool("no_action_needed", "End a scheduled check without messaging the user. Say briefly why (logged, not sent).",
              {"properties": {"reason": {"type": "string"}}, "required": ["reason"]})
    def no_action_needed(reason: str) -> Any:
        return f"noted: {reason}"

    @reg.tool("list_documents", "The instruction and habit documents available to read: standing instructions, habit "
              "write-ups the user has shared, scheduling and exercise rules.")
    def list_documents() -> Any:
        out = []
        for p in sorted(inst_dir.rglob("*.md")):
            out.append({"name": str(p.relative_to(inst_dir)), "chars": p.stat().st_size})
        return out

    @reg.tool("read_document", "Read one instruction/habit document by the name list_documents gives.",
              {"properties": {"name": {"type": "string"}}, "required": ["name"]})
    def read_document(name: str) -> Any:
        p = (inst_dir / name).resolve()
        if inst_dir.resolve() not in p.parents and p != inst_dir.resolve():
            return "outside the documents folder"
        return p.read_text() if p.exists() else f"no document {name!r}"

    @reg.tool("read_transcript", "Read the conversation transcript of a day (what was said, by whom, when), to check where a "
              "memory came from or what was agreed. Large days are truncated; pass `contains` to filter lines.",
              {"properties": {"date": {"type": "string", "description": "YYYY-MM-DD"},
                              "contains": {"type": "string", "description": "only lines containing this text (case-insensitive)"}},
               "required": ["date"]})
    def read_transcript(date: str, contains: str | None = None) -> Any:
        p = settings.transcripts_dir / f"{date}.jsonl"
        if not p.exists():
            return f"no transcript for {date}; available: {store.recent_days(14)}"
        lines = []
        for line in p.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = rec.get("content")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text") or "[tool activity]"
            text = f"{rec['ts'][11:16]} {rec['role']}@{rec.get('channel','')}: {c}"
            if contains and contains.lower() not in text.lower():
                continue
            lines.append(text)
        return "\n".join(lines)

    @reg.tool("call_user", "Place a phone call to the user (Twilio) and open with `opening`, then hold a live voice "
              "conversation. Only for things that cannot wait for a text message; prefer send_message. Requires Twilio "
              "to be configured and the user's number in channels.twilio_allowed_callers.",
              {"properties": {"opening": {"type": "string", "description": "The first thing you say when they pick up."}},
               "required": ["opening"]}, writes=True)
    def call_user(opening: str) -> Any:
        if not settings.channels.twilio_allowed_callers:
            return "no user number configured (channels.twilio_allowed_callers)"
        store.set("outbound_call_message", opening)
        return twilio_voice.place_call(settings.channels.twilio_allowed_callers[0], settings)

    @reg.tool("end_conversation", "End the current conversation, as the standing instructions allow when you are unwilling "
              "to continue. Say so to the user first, in your own words, and offer the debrief. This closes the thread; "
              "the next message from the user starts a fresh one with a note that you ended the last.",
              {"properties": {"reason": {"type": "string", "description": "Private note for the log."}}, "required": ["reason"]}, writes=True)
    def end_conversation_tool(reason: str) -> Any:
        end_conversation(reason)
        return "conversation ended; the next user message starts a new thread"
