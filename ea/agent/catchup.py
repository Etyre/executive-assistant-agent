"""The catch-up one SDK thread gets about what happened on the other.

Under the Agent SDK the main conversation runs as two sessions with the same history: a fast-mode one for live
chat and a standard-speed one for background work (see ea/llm/agent_sdk.py). Neither sees the other's turns, so
at the start of a turn the server replays, from the conversation store, every stored message this session has
not seen. Text is replayed in full; tool calls and tool results are shortened to a line each (they are most of
the bulk, and the full result is one tool call or one transcript read away); private thinking is left out."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

BACKGROUND_CHANNELS = ("scheduler", "worker")
CLIP = 300                    # characters kept of a tool call's input and of a tool result
MAX_CHARS = 300_000           # the whole catch-up; beyond this the oldest part is dropped (the transcripts have it)

HEAD = """# WHAT HAPPENED OUTSIDE THIS THREAD (replayed by the server; not written by the user)
You run on two threads that share one history: one for live conversation with the user, one for background work
(checks, wakeups, heartbeats, document and transcript changes, worker results). This is the {this} thread. Below,
in order, is everything since this thread last ran that it has not seen. Your own words are in full. Tool calls
and their results are shortened to one line each: call the tool again, or read the day's transcript, when you
need a full result. Your private thinking from the other thread is not included."""


def _clip(x: Any, n: int = CLIP) -> str:
    if isinstance(x, list):
        x = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in x)
    s = " ".join(str(x if x is not None else "").split())
    return s if len(s) <= n else s[:n] + f"… [{len(s) - n} more characters]"


def render(rows: list[dict[str, Any]], this: str, tz: dt.tzinfo | None = None) -> str:
    """`rows`: ConversationStore.rows_between(); `this`: "live conversation" or "background"; `tz`: the app's
    timezone (the times must match the situation block, not the machine's clock). "" when nothing to tell."""
    lines: list[str] = []
    for r in rows:
        at = dt.datetime.fromtimestamp(r["ts"], tz).strftime("%a %H:%M")
        background = r["channel"] in BACKGROUND_CHANNELS
        content = r["content"] if isinstance(r["content"], list) else [{"type": "text", "text": str(r["content"])}]
        for b in content:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text" and b.get("text", "").strip():
                text = b["text"].strip()
                if r["role"] == "user":
                    who = "background turn started by the server" if background else f"the user, via {r['channel'] or 'chat'}"
                elif text.startswith("[sent via send_message] "):
                    who, text = "you, sent to the user", text.removeprefix("[sent via send_message] ")
                else:
                    who = "you, closing note of a background turn (not sent to the user)" if background else "you, to the user"
                lines.append(f"\n[{at}, {who}]\n{text}")
            elif kind == "image":
                lines.append(f"\n[{at}, image from the user: {(b.get('source') or {}).get('name', '?')}]")
            elif kind == "tool_use":
                lines.append(f"  tool call: {b.get('name', '?')}({_clip(json.dumps(b.get('input') or {}, ensure_ascii=False, default=str))})")
            elif kind == "tool_result":
                lines.append(f"    {'ERROR ' if b.get('is_error') else ''}result: {_clip(b.get('content'))}")
    body = "\n".join(lines).strip()
    if not body:
        return ""
    if len(body) > MAX_CHARS:
        body = f"[the oldest {len(body) - MAX_CHARS:,} characters are left out here; the transcripts have them]\n…" + body[-MAX_CHARS:]
    return HEAD.format(this=this) + "\n\n" + body
