"""Memory tools. The store stamps dates itself; the agent only supplies content. `conversation_date` is the
date of the chat the memory came from (defaults to today), so a memory written about something said last
week points at the right transcript."""
from __future__ import annotations

from typing import Any

from ..memory.store import MemoryStore
from .registry import ToolRegistry

DATE = {"type": "string", "description": "YYYY-MM-DD of the conversation this comes from; omit for today."}


def register(reg: ToolRegistry, store: MemoryStore, channel_getter) -> None:
    @reg.tool("memory_search", "Full-text search over your memories. Returns name, title and a snippet; read the ones that matter.",
              {"properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}},
               "required": ["query"]})
    def memory_search(query: str, limit: int = 8) -> Any:
        return store.search(query, limit)

    @reg.tool("memory_read", "Read one memory in full (body plus its source and edit dates).",
              {"properties": {"name": {"type": "string"}}, "required": ["name"]})
    def memory_read(name: str) -> Any:
        m = store.read(name)
        return m if m else f"no memory named {name!r}"

    @reg.tool("memory_list", "List every memory: name, title, tags, dates. Use memory_search when you know what you are looking for.")
    def memory_list() -> Any:
        return [{k: m[k] for k in ("name", "title", "tags", "created", "edits")} for m in store.list()]

    @reg.tool("memory_write", "Create a memory (Markdown body). The store records today's date as the edit date and the "
              "conversation date + channel as the source, so it can be traced back to the transcript. If a memory with the "
              "same name exists this updates it instead (keeping its history).",
              {"properties": {"title": {"type": "string"}, "body": {"type": "string"},
                              "tags": {"type": "array", "items": {"type": "string"}},
                              "name": {"type": "string", "description": "Optional slug; defaults to a slug of the title."},
                              "conversation_date": DATE},
               "required": ["title", "body"]}, writes=True)
    def memory_write(title: str, body: str, tags: list[str] | None = None, name: str | None = None,
                     conversation_date: str | None = None) -> Any:
        m = store.write(title, body, tags, conversation_date, channel_getter(), name)
        return {"saved": m["name"], "path": m["path"], "sources": m["sources"], "edits": m["edits"]}

    @reg.tool("memory_update", "Edit a memory: replace the body, or append to it, and/or add tags. Earlier source and edit "
              "dates are kept and today's are added.",
              {"properties": {"name": {"type": "string"}, "body": {"type": "string", "description": "New full body (replaces)."},
                              "append": {"type": "string", "description": "Text to add at the end instead of replacing."},
                              "tags": {"type": "array", "items": {"type": "string"}}, "title": {"type": "string"},
                              "conversation_date": DATE},
               "required": ["name"]}, writes=True)
    def memory_update(name: str, body: str | None = None, append: str | None = None, tags: list[str] | None = None,
                      title: str | None = None, conversation_date: str | None = None) -> Any:
        m = store.update(name, body=body, append=append, tags=tags, source_date=conversation_date,
                         channel=channel_getter(), title=title)
        return {"saved": m["name"], "sources": m["sources"], "edits": m["edits"]}

    @reg.tool("memory_archive", "Move a memory out of the active set (kept under _archive/, never deleted).",
              {"properties": {"name": {"type": "string"}}, "required": ["name"]}, writes=True)
    def memory_archive(name: str) -> Any:
        return "archived" if store.archive(name) else f"no memory named {name!r}"
