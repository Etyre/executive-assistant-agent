"""The provider-neutral shape of a model call.

Messages use the Anthropic content-block convention throughout the agent (text, tool_use, tool_result,
thinking). A provider that speaks another dialect translates at its edge. That is what lets the model be
swapped for a locally hosted open-weight one without touching the agent loop, tools or memory.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Protocol


@dataclasses.dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclasses.dataclass
class LLMResponse:
    content: list[dict[str, Any]]            # content blocks, Anthropic shape
    stop_reason: str                          # end_turn | tool_use | max_tokens | refusal | ...
    usage: dict[str, Any] = dataclasses.field(default_factory=dict)
    model: str = ""

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class Provider(Protocol):
    name: str

    def complete(self, *, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                 tools: list[ToolSpec], max_tokens: int,
                 on_text: Callable[[str], None] | None = None) -> LLMResponse:
        """One model call. With `on_text`, text is streamed to it as it is generated (a provider that cannot
        stream may call it once with the whole text); the returned response is complete either way."""
        ...


class LLMError(RuntimeError):
    pass
