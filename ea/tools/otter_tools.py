"""Read-only tools over the user's Otter.ai voice-recording transcripts (walking journal, verbal notes,
recorded conversations). The index merges Otter's API with the Logseq archive; see connectors/otter.py."""
from __future__ import annotations

from typing import Any

from ..connectors.otter import Otter
from .registry import ToolRegistry


def register(reg: ToolRegistry, otter: Otter) -> None:
    def _with_warning(result: Any, warn: str | None) -> Any:
        if warn is None:
            return result
        return {"warning": warn, "results": result}

    @reg.tool("otter_recent", "Otter.ai recordings (voice transcripts) by date: the last N days, or N days from start_date. "
              "Returns date, time, title, folder ('walking journal', 'audio notes' or none), length and a short preview "
              "per recording, with the otid for otter_transcript. Folder filter is optional.",
              {"properties": {"days": {"type": "integer", "minimum": 1, "maximum": 366, "description": "How many days to cover."},
                              "start_date": {"type": "string", "description": "YYYY-MM-DD; omit to count back from today."},
                              "folder": {"type": "string", "description": "'walking journal', 'audio notes', or '' for recordings in no folder."},
                              "limit": {"type": "integer", "minimum": 1, "maximum": 500}},
               "required": ["days"]})
    def otter_recent(days: int, start_date: str | None = None, folder: str | None = None, limit: int = 50) -> Any:
        warn = otter.ensure_fresh()
        return _with_warning(otter.recent(days, folder=folder, limit=limit, start_date=start_date), warn)

    @reg.tool("otter_search", "Full-text search over every Otter transcript (back to January 2024). FTS5 syntax: plain words "
              "are ANDed, use OR, \"quoted phrases\", prefix*. Each hit has a snippet with the matched words in «», the date "
              "and the otid; call otter_transcript for the full text.",
              {"properties": {"query": {"type": "string"},
                              "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                              "days": {"type": "integer", "minimum": 1, "description": "Only recordings from the last N days."},
                              "folder": {"type": "string"}},
               "required": ["query"]})
    def otter_search(query: str, limit: int = 10, days: int | None = None, folder: str | None = None) -> Any:
        warn = otter.ensure_fresh()
        return _with_warning(otter.search(query, limit=limit, days=days, folder=folder), warn)

    @reg.tool("otter_transcript", "The full text of one Otter recording by otid (speaker-labelled paragraphs, timestamps when "
              "Otter supplied them), plus Otter's summary if any. Long recordings are paged: pass next_offset from the "
              "previous result as offset to continue.",
              {"properties": {"otid": {"type": "string"},
                              "offset": {"type": "integer", "minimum": 0},
                              "max_chars": {"type": "integer", "minimum": 1000, "maximum": 50000}},
               "required": ["otid"]})
    def otter_transcript(otid: str, offset: int = 0, max_chars: int = 40_000) -> Any:
        otter.ensure_fresh()
        rec = otter.get(otid, offset=offset, max_chars=max_chars)
        return rec if rec is not None else {"error": f"no recording with otid {otid!r} in the index"}

    @reg.tool("otter_status", "How complete the Otter transcript index is: how many recordings, the date range, whether the "
              "Otter login is configured and when it last synced. Use when a recording the user mentions is missing.")
    def otter_status() -> Any:
        warn = otter.ensure_fresh()
        st = otter.status()
        if warn:
            st["warning"] = warn
        return st
