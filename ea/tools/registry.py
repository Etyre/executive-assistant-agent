"""Tools the model can call. Each is a plain function plus a JSON schema; results are returned as text
(JSON for structured data) so any provider can consume them. Errors are returned to the model as text,
never raised, so one failing source does not end the turn."""
from __future__ import annotations

import dataclasses
import json
import logging
import traceback
from typing import Any, Callable

from ..llm.base import ToolSpec

log = logging.getLogger(__name__)
MAX_RESULT_CHARS = 60_000


@dataclasses.dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]
    writes: bool = False          # changes something outside the agent (tracker, messages)

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def tool(self, name: str, description: str, schema: dict[str, Any] | None = None, writes: bool = False):
        """Decorator. `schema` is the JSON schema of the input object (properties/required)."""
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            props = (schema or {}).get("properties", {})
            self.add(Tool(name, description, {"type": "object", "properties": props,
                                              "required": (schema or {}).get("required", []),
                                              "additionalProperties": False}, fn, writes))
            return fn
        return deco

    def specs(self, exclude: set[str] | None = None) -> list[ToolSpec]:
        return [t.spec() for n, t in sorted(self._tools.items()) if not exclude or n not in exclude]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (text result, is_error)."""
        t = self._tools.get(name)
        if not t:
            return f"unknown tool {name!r}; available: {', '.join(self.names())}", True
        try:
            out = t.fn(**(args or {}))
        except TypeError as e:
            return f"bad arguments for {name}: {e}", True
        except Exception as e:  # noqa: BLE001 - the model gets the error and carries on
            log.warning("tool %s failed: %s\n%s", name, e, traceback.format_exc())
            return f"{type(e).__name__}: {e}", True
        return _render(out), False


def _render(out: Any) -> str:
    if out is None:
        return "ok"
    if isinstance(out, str):
        text = out
    else:
        text = json.dumps(out, ensure_ascii=False, default=str, indent=None)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…[truncated; {len(text)} chars in all - ask for a narrower range]"
    return text
