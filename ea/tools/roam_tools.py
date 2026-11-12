"""Read-only tools over the user's Roam Research graph (an export in data/roam/; see connectors/roam.py)."""
from __future__ import annotations

from typing import Any

from ..connectors.roam import Roam
from .registry import ToolRegistry


def register(reg: ToolRegistry, roam: Roam) -> None:
    def _warned(result: Any, warn: str | None) -> Any:
        return result if warn is None else {"warning": warn, "results": result}

    @reg.tool("roam_search", "Full-text search over every block of the user's Roam graph (notes, journals, project pages). "
              "FTS5 syntax: plain words are ANDed, use OR, \"quoted phrases\", prefix*. Each hit: page, snippet with the "
              "matched words in «», the block text, its parent blocks for context, the journal date if any.",
              {"properties": {"query": {"type": "string"},
                              "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                              "days": {"type": "integer", "minimum": 1, "description": "Only blocks edited, or journal pages dated, in the last N days."},
                              "page": {"type": "string", "description": "Only pages whose title contains this."}},
               "required": ["query"]})
    def roam_search(query: str, limit: int = 10, days: int | None = None, page: str | None = None) -> Any:
        warn = roam.ensure_fresh()
        return _warned(roam.search(query, limit=limit, days=days, page=page), warn)

    @reg.tool("roam_page", "One Roam page as an outline (block refs resolved inline). Journal pages are titled like "
              "'November 6th, 2026'. Long pages are paged: pass next_offset as offset to continue. If the title is not "
              "exact, similar titles are suggested.",
              {"properties": {"title": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
                              "max_chars": {"type": "integer", "minimum": 1000, "maximum": 60000}},
               "required": ["title"]})
    def roam_page(title: str, offset: int = 0, max_chars: int = 40_000) -> Any:
        warn = roam.ensure_fresh()
        out = roam.page(title, offset=offset, max_chars=max_chars)
        return _warned(out if out is not None else {"error": f"no page titled {title!r}"}, warn)

    @reg.tool("roam_recent", "What the user wrote in Roam lately: journal pages in the last N days (or N days from "
              "start_date) with previews, and the pages whose blocks were edited in that window.",
              {"properties": {"days": {"type": "integer", "minimum": 1, "maximum": 366},
                              "start_date": {"type": "string", "description": "YYYY-MM-DD; omit to count back from today."},
                              "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
               "required": ["days"]})
    def roam_recent(days: int, start_date: str | None = None, limit: int = 40) -> Any:
        warn = roam.ensure_fresh()
        return _warned(roam.recent(days, limit=limit, start_date=start_date), warn)

    @reg.tool("roam_backlinks", "Every block anywhere in the graph that references [[title]] or #title: Roam's "
              "'Linked References' for a page, newest first.",
              {"properties": {"title": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
               "required": ["title"]})
    def roam_backlinks(title: str, limit: int = 40) -> Any:
        warn = roam.ensure_fresh()
        return _warned(roam.backlinks(title, limit=limit), warn)

    @reg.tool("roam_pages", "List page titles in the graph (largest first), optionally only those containing a word; "
              "journals=true lists daily-note pages instead.",
              {"properties": {"contains": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                              "journals": {"type": "boolean"}}})
    def roam_pages(contains: str = "", limit: int = 50, journals: bool = False) -> Any:
        warn = roam.ensure_fresh()
        return _warned(roam.pages(contains, limit=limit, journals=journals), warn)

    @reg.tool("roam_status", "Whether a Roam export is present and indexed: files, page/block counts, journal date range, "
              "last edit in the export. Use when the user mentions a note you cannot find.")
    def roam_status() -> Any:
        warn = roam.ensure_fresh()
        st = roam.status()
        if warn:
            st["warning"] = warn
        return st
