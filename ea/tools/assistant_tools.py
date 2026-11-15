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


def usage_keys() -> tuple[str, ...]:
    from .. import usage
    return usage.GROUP_KEYS


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

    @reg.tool("schedule_wakeup", "Wake yourself up at a given local time to run a proactive turn on `prompt` (written to "
              "yourself: what to look at and what would warrant messaging). You get all your tools; nothing reaches the user "
              "unless that turn calls send_message. Use it for follow-ups ('did the 14:00 focus block happen?'), watches "
              "('check whether task 76 got a plan'), or a repeating check with every_minutes. Not for plain reminders: "
              "set_reminder sends text without a turn.",
              {"properties": {"when": {"type": "string", "description": "ISO local datetime, e.g. 2026-11-05T15:00"},
                              "prompt": {"type": "string", "description": "What you will do when you wake up."},
                              "every_minutes": {"type": "integer", "minimum": 5, "description": "Repeat at this interval until cancelled (optional)."}},
               "required": ["when", "prompt"]}, writes=True)
    def schedule_wakeup(when: str, prompt: str, every_minutes: int | None = None) -> Any:
        due = dt.datetime.fromisoformat(when)
        if due.tzinfo is None:
            due = due.replace(tzinfo=tz)
        rid = store.add_reminder(due, prompt, kind="wakeup", every_minutes=every_minutes)
        return {"wakeup_id": rid, "due": due.isoformat(timespec="minutes"), "every_minutes": every_minutes}

    @reg.tool("list_reminders", "Pending reminders (text to the user) and wakeups (your own scheduled turns), with ids.")
    def list_reminders() -> Any:
        return [{"id": r["id"], "kind": r.get("kind") or "message", "due": dt.datetime.fromtimestamp(r["due_ts"], tz).isoformat(timespec="minutes"),
                 "every_minutes": r.get("every_minutes"), "text": r["text"]} for r in store.pending_reminders()]

    @reg.tool("cancel_reminder", "Cancel a pending reminder or wakeup by id (repeating ones stop for good).",
              {"properties": {"reminder_id": {"type": "integer"}}, "required": ["reminder_id"]}, writes=True)
    def cancel_reminder(reminder_id: int) -> Any:
        return "cancelled" if store.cancel_reminder(reminder_id) else "no such pending reminder or wakeup"

    @reg.tool("send_message", "Send the user a message now on the chat channel. In a normal chat turn your reply is already "
              "delivered, so this is for scheduled checks (where nothing is sent unless you call it) and for a second, "
              "separate message.",
              {"properties": {"text": {"type": "string"}}, "required": ["text"]}, writes=True)
    def send_message_tool(text: str) -> Any:
        send_message(text)
        return "sent"

    @reg.tool("model_usage", "What your own model calls cost: tokens and estimated cost split by speed (fast mode vs standard) "
              "for today, 7 days, 30 days and since tracking began; a per-day table of what was charged to the user's "
              "usage credits (chat, keep-alive, cold starts) and what the plan covered; and where the money goes by "
              "component (uncached input, cache reads, cache writes, output) and by kind of turn. Your memory "
              "`model-costs` explains the numbers.",
              {"properties": {"days": {"type": "integer", "description": "days covered by the per-day and breakdown tables (default 7)"}}})
    def model_usage(days: int = 7) -> Any:
        from .. import usage
        n = max(1, min(int(days or 7), 60))
        return "\n\n".join([usage.report(store, dt.datetime.now(tz)), usage.daily_report(store, n, tz), usage.breakdown_report(store, n, tz)])

    @reg.tool("model_usage_query", "Look up your recorded model calls and aggregate them your own way. Every call since "
              "tracking began is kept (speed, kind of turn, model, tokens and cost), so any day can be looked up. Give a "
              "date range and what to group by; each row has the number of calls, tokens and dollars for each cost "
              "component (input, cache_read, cache_write_5m, cache_write_1h, output), the total at list prices (usd), "
              "what was charged beyond the plan (charged_usd) and the number of cold starts.",
              {"properties": {"start": {"type": "string", "description": "first day, YYYY-MM-DD (default: 6 days before `end`)"},
                              "end": {"type": "string", "description": "last day, YYYY-MM-DD, inclusive (default: today)"},
                              "group_by": {"type": "array", "items": {"type": "string", "enum": list(usage_keys())},
                                           "description": "in order, e.g. [\"day\", \"speed\"] (the default), [\"mode\"], [\"hour\", \"mode\"]; \"call\" lists single model calls"}}})
    def model_usage_query(start: str | None = None, end: str | None = None, group_by: list[str] | None = None) -> Any:
        from .. import usage
        return usage.query(store, start, end, group_by, tz)

    @reg.tool("no_action_needed", "End a scheduled check without messaging the user. Say briefly why (logged, not sent).",
              {"properties": {"reason": {"type": "string"}}, "required": ["reason"]})
    def no_action_needed(reason: str) -> Any:
        return f"noted: {reason}"

    def _roots() -> list[tuple[str, pathlib.Path, str]]:
        """(prefix, folder, note): the instructions folder plus every configured document root."""
        out = [("", inst_dir, "instructions and habit documents")]
        for r in settings.document_roots:
            out.append((r.name, pathlib.Path(r.path).expanduser(), r.note))
        return out

    @reg.tool("list_documents", "The documents available to read, live from disk: the standing instructions and habit "
              "write-ups, plus every configured document root (e.g. the Markdown sources of the user's website, "
              "under its own prefix). Each entry has its name for read_document, size and last-modified date, so "
              "you can see what changed recently.")
    def list_documents() -> Any:
        out = []
        for prefix, folder, note in _roots():
            if not folder.exists():
                out.append({"root": prefix or "instructions", "error": f"folder {folder} not found"})
                continue
            for p in sorted(folder.rglob("*.md")):
                if p.name.startswith(".") or p.stem == "" or "/." in str(p.relative_to(folder)):
                    continue
                rel = str(p.relative_to(folder))
                st = p.stat()
                out.append({"name": f"{prefix}/{rel}" if prefix else rel, "chars": st.st_size,
                            "modified": dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="minutes"),
                            **({"root": prefix, "about": note} if prefix and rel == sorted(x.relative_to(folder) for x in folder.rglob("*.md"))[0].as_posix() else {})})
        return out

    @reg.tool("read_document", "Read one document by the name list_documents gives (e.g. 'habits/x.md' or "
              "'elityre.com/Sleep.md'). Always the current file on disk.",
              {"properties": {"name": {"type": "string"}}, "required": ["name"]})
    def read_document(name: str) -> Any:
        name = name.strip().lstrip("/")
        folder, rel = inst_dir, name
        for prefix, root, _note in _roots():
            if prefix and (name == prefix or name.startswith(prefix + "/")):
                folder, rel = root, name[len(prefix) + 1:]
                break
        p = (folder / rel).resolve()
        if folder.resolve() not in p.parents and p != folder.resolve():
            return "outside the documents folder"
        if not p.exists() or not p.is_file():
            return f"no document {name!r}; see list_documents"
        return p.read_text(errors="replace")

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

    @reg.tool("integrations_status", "The live state of every integration the user wants you to have: what each is for, "
              "whether it works right now (ok / partial / missing / off), what it still needs (env vars, config keys, "
              "accounts), how to obtain that, and who has to act. Use it to keep your integrations record in memory "
              "accurate and to know what to ask the user for.")
    def integrations_status() -> Any:
        from .. import integrations
        return integrations.status(settings)

    @reg.tool("end_conversation", "End the current conversation, as the standing instructions allow when you are unwilling "
              "to continue. Say so to the user first, in your own words, and offer the debrief. This closes the thread; "
              "the next message from the user starts a fresh one with a note that you ended the last.",
              {"properties": {"reason": {"type": "string", "description": "Private note for the log."}}, "required": ["reason"]}, writes=True)
    def end_conversation_tool(reason: str) -> Any:
        end_conversation(reason)
        return "conversation ended; the next user message starts a new thread"
