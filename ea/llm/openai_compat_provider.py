"""An OpenAI-compatible chat-completions server: Ollama, vLLM, LM Studio, llama.cpp's server, and so on.

This is the path for running an open-weight model on the Mac mini. It translates the agent's
Anthropic-shaped history into chat-completions messages and the reply back, so nothing else changes.
Thinking blocks from a previous Claude turn are dropped (they are opaque to other models).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from .base import LLMError, LLMResponse, ToolSpec

log = logging.getLogger(__name__)


def to_openai_messages(system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sys_text = "\n\n".join(b.get("text", "") for b in system if b.get("type") == "text").strip()
    if sys_text:
        out.append({"role": "system", "content": sys_text})
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "system" and isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in content if b.get("type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user: text and/or tool results
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            for b in content:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(c or "")})
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def from_openai_message(msg: dict[str, Any], finish_reason: str | None) -> LLMResponse:
    content: list[dict[str, Any]] = []
    text = msg.get("content")
    if isinstance(text, list):   # some servers return parts
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        content.append({"type": "tool_use", "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                        "name": fn.get("name", ""), "input": args})
    stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else (
        "max_tokens" if finish_reason == "length" else "end_turn")
    return LLMResponse(content=content, stop_reason=stop)


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.http = httpx.Client(timeout=timeout)

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in tools]

    def complete(self, *, system: list[dict[str, Any]], messages: list[dict[str, Any]],
                 tools: list[ToolSpec], max_tokens: int) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": to_openai_messages(system, messages),
                                "max_tokens": max_tokens}
        if tools:
            body["tools"] = self._tools(tools)
        try:
            r = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise LLMError(f"{self.base_url} returned {e.response.status_code}: {e.response.text[:300]}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"could not reach {self.base_url}: {e}") from e
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        resp = from_openai_message(choice.get("message") or {}, choice.get("finish_reason"))
        resp.usage = data.get("usage") or {}
        resp.model = data.get("model", self.model)
        return resp
