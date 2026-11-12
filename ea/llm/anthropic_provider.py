"""Claude via the official Anthropic SDK.

Defaults follow the current API: adaptive thinking (always on for Claude Opus 5.5), `output_config.effort`
for depth, prompt caching on the stable system block, and server-side refusal fallbacks so a safety
classifier declining a turn is routed to another Claude model rather than surfacing as an error.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import anthropic

from .base import LLMError, LLMResponse, ToolSpec

log = logging.getLogger(__name__)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "high", fallbacks: bool = True,
                 client: anthropic.Anthropic | None = None, thinking_display: str = "summarized"):
        self.model = model
        self.effort = effort
        self.thinking_display = thinking_display      # summarized (readable, for the chain-of-thought pane) | omitted
        self.fallbacks = fallbacks
        self.client = client or anthropic.Anthropic(max_retries=3)

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]

    @staticmethod
    def _with_history_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Mark the last conversation message (not a trailing system message) as a cache breakpoint, so the
        whole history up to it is read from cache next time. Works on copies; stored history is untouched."""
        out = list(messages)
        for i in range(len(out) - 1, -1, -1):
            m = out[i]
            if m.get("role") == "system":
                continue
            content = m.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if isinstance(content, list) and content and isinstance(content[-1], dict) \
                    and content[-1].get("type") in ("text", "tool_result", "tool_use"):
                content = list(content)
                content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
                out[i] = {**m, "content": content}
            break
        return out

    def complete(self, *, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                 tools: list[ToolSpec], max_tokens: int,
                 on_text: Callable[[str], None] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=max_tokens, system=system, messages=self._with_history_breakpoint(messages),
            thinking={"type": "adaptive", "display": self.thinking_display}, output_config={"effort": self.effort},
        )
        if tools:
            kwargs["tools"] = self._tools(tools)
        if self.fallbacks:
            kwargs.update(betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"})
        api = self.client.beta.messages if self.fallbacks else self.client.messages
        try:
            if on_text is None:
                resp = api.create(**kwargs)
            else:
                # The stream helper accumulates the full message; the caller sees text as it is generated.
                with api.stream(**kwargs) as stream:
                    for event in stream:
                        if event.type == "text" and event.text:
                            on_text(event.text)
                    resp = stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited by Anthropic: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"could not reach Anthropic: {e}") from e
        content = [b.model_dump(exclude_none=True) for b in resp.content]
        stop = resp.stop_reason or "end_turn"
        if stop == "refusal":
            detail = getattr(resp, "stop_details", None)
            why = getattr(detail, "explanation", None) if detail else None
            content = [{"type": "text", "text": "I can't help with that particular request." + (f" ({why})" if why else "")}]
        usage = resp.usage.model_dump(exclude_none=True) if resp.usage else {}
        return LLMResponse(content=content, stop_reason=stop, usage=usage, model=getattr(resp, "model", self.model))
