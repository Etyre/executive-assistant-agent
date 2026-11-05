"""Captures the exact HTTP request the Anthropic SDK builds for one agent call (no network) and checks the
API shapes the skill reference requires for Claude Opus 5.5: adaptive thinking, output_config.effort, the
fallbacks beta, cached stable system block, the trailing system message, and the tool schema."""
import json

import anthropic
import httpx2
import pytest

from ea.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from ea.llm.base import ToolSpec


def _capture_client(captured):
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2}})
    return anthropic.Anthropic(api_key="test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)), max_retries=0)


@pytest.mark.parametrize("fallbacks", [True, False])
def test_request_shape(fallbacks):
    cap = {}
    p = AnthropicProvider(model="claude-opus-5-5", effort="high", fallbacks=fallbacks, client=_capture_client(cap))
    system = [{"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}}]
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}, {"role": "system", "content": "NOW 12:00"}]
    tools = [ToolSpec("memory_search", "search", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False})]
    r = p.complete(system=system, messages=msgs, tools=tools, max_tokens=1234)
    assert r.text == "hello" and r.stop_reason == "end_turn" and r.usage["input_tokens"] == 10
    b = cap["body"]
    assert b["model"] == "claude-opus-5-5" and b["max_tokens"] == 1234
    assert b["thinking"] == {"type": "adaptive"} and "budget_tokens" not in json.dumps(b)
    assert b["output_config"] == {"effort": "high"}
    assert b["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert b["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}      # history breakpoint
    assert b["messages"][1] == {"role": "system", "content": "NOW 12:00"}
    assert b["tools"][0]["name"] == "memory_search" and b["tools"][0]["input_schema"]["required"] == ["query"]
    assert "temperature" not in b and "top_p" not in b and "tool_choice" not in b
    if fallbacks:
        assert b["fallbacks"] == "default" and FALLBACK_BETA in cap["headers"].get("anthropic-beta", "")
    else:
        assert "fallbacks" not in b and "anthropic-beta" not in cap["headers"]


def test_refusal_becomes_text():
    cap = {}
    def handler(request):
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                           "content": [], "stop_reason": "refusal", "stop_sequence": None,
                                           "stop_details": {"type": "refusal", "category": "cyber", "explanation": "nope"},
                                           "usage": {"input_tokens": 1, "output_tokens": 0}})
    client = anthropic.Anthropic(api_key="t", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)), max_retries=0)
    r = AnthropicProvider(fallbacks=False, client=client).complete(system=[], messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=10)
    assert r.stop_reason == "refusal" and "can't help" in r.text
