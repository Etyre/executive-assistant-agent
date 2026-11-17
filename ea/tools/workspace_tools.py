"""The shared workspace (whiteboard) the user and the agent both edit. Separate from the chat."""
from __future__ import annotations

from typing import Any

from ..workspace import Workspace
from .registry import ToolRegistry


def register(reg: ToolRegistry, ws: Workspace, show: "callable | None" = None, docs: "Documents | None" = None,
             hide: "callable | None" = None) -> None:
    """`show(doc)` (set by the server) asks every open app to bring that document up in the workspace pane;
    `hide()` asks them to close the pane."""
    def _show(name: str = "workspace") -> None:
        if show is not None:
            show(name)

    @reg.tool("workspace_read", "Read the shared workspace: one Markdown page you and the user both edit, shown beside the "
              "chat in the app. It is a whiteboard, not a record: what is on it should be current. Use it for what you are "
              "working on together right now (a plan, a draft, a list, open questions); update what changed and remove "
              "what is done or no longer relevant (every version is kept, so removing is safe). Returns the text, its "
              "version id, and who last changed it.")
    def workspace_read() -> Any:
        return ws.read()

    @reg.tool("workspace_edit", "Change part of the workspace: replace one exact passage (`find`, which must occur exactly "
              "once) with `replace`. Prefer this over rewriting the whole page so the user's own edits are kept.",
              {"properties": {"find": {"type": "string"}, "replace": {"type": "string"},
                              "note": {"type": "string", "description": "A few words on what you changed (shown in the history)."}},
               "required": ["find", "replace"]}, writes=True)
    def workspace_edit(find: str, replace: str, note: str = "") -> Any:
        try:
            out = ws.edit(find, replace, by="agent", note=note)
        except ValueError as e:
            return {"error": str(e)}
        _show()
        return {"ok": True, "version": out["version"], "chars": out["chars"]}

    @reg.tool("workspace_remove", "Delete one exact passage from the workspace (`find` must occur exactly once): a done item, "
              "a stale note, a section that no longer matters. Old versions stay in the history.",
              {"properties": {"find": {"type": "string"}, "note": {"type": "string"}}, "required": ["find"]}, writes=True)
    def workspace_remove(find: str, note: str = "") -> Any:
        cur = ws.read()["text"]
        n = cur.count(find)
        if n != 1:
            return {"error": f"the passage occurs {n} times; it must occur exactly once"}
        new = cur.replace(find, "", 1)
        import re
        new = re.sub(r"\n{3,}", "\n\n", new).strip("\n")
        new = new + "\n" if new else ""
        out = ws.write(new, by="agent", note=note or "removed")
        _show()
        return {"ok": True, "version": out["version"], "chars": out["chars"]}

    @reg.tool("workspace_append", "Add text at the end of the workspace (a new section, a list, a note). Keep the page short: "
              "add only what is live, and take things off as they are done.",
              {"properties": {"text": {"type": "string"}, "note": {"type": "string"}}, "required": ["text"]}, writes=True)
    def workspace_append(text: str, note: str = "") -> Any:
        out = ws.append(text, by="agent", note=note)
        _show()
        return {"ok": True, "version": out["version"], "chars": out["chars"]}

    @reg.tool("workspace_write", "Replace the whole workspace with new text. Only when restructuring the page; pass the "
              "version you read so the user's edits in between are not lost (a mismatch is refused with the current text).",
              {"properties": {"text": {"type": "string"}, "base_version": {"type": "string"}, "note": {"type": "string"}},
               "required": ["text", "base_version"]}, writes=True)
    def workspace_write(text: str, base_version: str, note: str = "") -> Any:
        from ..workspace import Conflict
        try:
            out = ws.write(text, by="agent", base_version=base_version, note=note or "rewrote")
        except Conflict as c:
            return {"error": "the workspace changed since that version; here is the current text", "current": c.current}
        _show()
        return {"ok": True, "version": out["version"], "chars": out["chars"]}

    @reg.tool("workspace_open", "Bring the workspace pane up in the user's app (it may be hidden). Use when you want to "
              "work on something there with the user, or when you have put something there they should look at. "
              "Editing the page opens it too.", writes=False)
    def workspace_open() -> Any:
        _show()
        return "workspace shown in the app"

    @reg.tool("workspace_close", "Close the workspace pane in the user's app, leaving just the chat. Nothing on the page "
              "is changed or lost, and workspace_open (or any edit) brings it back. Use when the user asks you to "
              "close it, or when you are both done with what was on it.", writes=False)
    def workspace_close() -> Any:
        if hide is None:
            return {"error": "no app is connected to close it in"}
        hide()
        return "workspace pane closed in the app"

    # ---------------------------------------------------------------- documents beyond the whiteboard
    if docs is not None:
        @reg.tool("document_list", "The shared documents: the whiteboard plus every page you or the user created (name, title, "
                  "size, last edit). They show in the app's workspace pane; the user can switch between them.")
        def document_list() -> Any:
            return docs.names()

        @reg.tool("document_read", "Read one shared document by name (document_list). Returns text, version, last editor.",
                  {"properties": {"name": {"type": "string"}}, "required": ["name"]})
        def document_read(name: str) -> Any:
            try:
                d = docs.get(name, create=False)
            except ValueError as e:
                return {"error": str(e)}
            return d.read() if d else {"error": f"no document {name!r}; document_list shows what exists"}

        @reg.tool("document_write", "Create a shared document, or replace one. A Markdown page: headings, lists, tables, "
                  "links, code; a ```mermaid fence (flowchart, sequence, gantt, mindmap, pie, timeline...) renders as a "
                  "diagram in the app. Give it a short descriptive name (e.g. 'DC trip plan'). To replace an existing page "
                  "pass the version you read; a mismatch is refused with the current text. Opens it in the app.",
                  {"properties": {"name": {"type": "string"}, "text": {"type": "string"},
                                  "base_version": {"type": "string", "description": "required when replacing an existing document"},
                                  "note": {"type": "string"}},
                   "required": ["name", "text"]}, writes=True)
        def document_write(name: str, text: str, base_version: str = "", note: str = "") -> Any:
            from ..workspace import Conflict
            try:
                existing = docs.get(name, create=False)
                if existing is not None and not base_version:
                    return {"error": f"document {name!r} exists; pass base_version (from document_read) to replace it, or use document_edit"}
                d = docs.get(name, default=text)
                out = d.write(text, by="agent", base_version=base_version or None, note=note or ("created" if existing is None else "rewrote"))
            except ValueError as e:
                return {"error": str(e)}
            except Conflict as c:
                return {"error": "the document changed since that version; here is the current text", "current": c.current}
            _show(d.name)
            return {"ok": True, "name": d.name, "version": out["version"], "chars": out["chars"]}

        @reg.tool("document_edit", "Change part of a shared document: replace one exact passage (`find` must occur exactly "
                  "once) with `replace`; an empty `replace` deletes the passage.",
                  {"properties": {"name": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"}, "note": {"type": "string"}},
                   "required": ["name", "find", "replace"]}, writes=True)
        def document_edit(name: str, find: str, replace: str, note: str = "") -> Any:
            try:
                d = docs.get(name, create=False)
                if d is None:
                    return {"error": f"no document {name!r}"}
                out = d.edit(find, replace, by="agent", note=note)
            except ValueError as e:
                return {"error": str(e)}
            _show(d.name)
            return {"ok": True, "version": out["version"], "chars": out["chars"]}

        @reg.tool("document_append", "Add text at the end of a shared document.",
                  {"properties": {"name": {"type": "string"}, "text": {"type": "string"}, "note": {"type": "string"}}, "required": ["name", "text"]}, writes=True)
        def document_append(name: str, text: str, note: str = "") -> Any:
            try:
                d = docs.get(name, create=False)
            except ValueError as e:
                return {"error": str(e)}
            if d is None:
                return {"error": f"no document {name!r}; document_write creates one"}
            out = d.append(text, by="agent", note=note)
            _show(d.name)
            return {"ok": True, "version": out["version"], "chars": out["chars"]}

        @reg.tool("document_delete", "Delete a shared document that is no longer needed (its last text stays in the history). "
                  "The whiteboard cannot be deleted.",
                  {"properties": {"name": {"type": "string"}}, "required": ["name"]}, writes=True)
        def document_delete(name: str) -> Any:
            try:
                return {"ok": docs.delete(name, by="agent")}
            except ValueError as e:
                return {"error": str(e)}

        @reg.tool("document_open", "Show a shared document in the user's app (the workspace pane switches to it).",
                  {"properties": {"name": {"type": "string"}}, "required": ["name"]})
        def document_open(name: str) -> Any:
            try:
                d = docs.get(name, create=False)
            except ValueError as e:
                return {"error": str(e)}
            if d is None:
                return {"error": f"no document {name!r}"}
            _show(d.name)
            return f"{d.name} shown in the app"

    @reg.tool("workspace_history", "Recent versions of the workspace: when, by whom (user/agent), and a note.",
              {"properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}})
    def workspace_history(limit: int = 20) -> Any:
        return ws.history(limit)
