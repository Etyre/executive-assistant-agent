"""Fast mode is on when, and only when, the user is waiting on the reply (chat, voice): never on checks,
wakeups, heartbeats, ingestion or workers. Covers the API request, the agent loop and the Agent SDK options."""
import json

import anthropic
import claude_agent_sdk as sdk
import httpx2

from ea import config
from ea.agent.core import Agent
from ea.llm.anthropic_provider import AnthropicProvider
from ea.llm.base import LLMResponse

OK = {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "hi"}],
      "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


def _provider(handler):
    client = anthropic.Anthropic(api_key="t", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)), max_retries=0)
    return AnthropicProvider(fallbacks=False, client=client)


def test_api_request_carries_speed_only_when_fast():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=OK)
    p = _provider(handler)
    kw = dict(system=[], messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=10)
    p.complete(**kw)
    p.complete(**kw, fast=True)
    assert "speed" not in bodies[0] and bodies[1]["speed"] == "fast"


def test_fast_rate_limit_is_redone_at_standard_speed():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if "speed" in bodies[-1]:
            return httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "fast mode limit"}})
        return httpx2.Response(200, json=OK)
    r = _provider(handler).complete(system=[], messages=[{"role": "user", "content": "x"}], tools=[], max_tokens=10, fast=True)
    assert r.text == "hi" and [("speed" in b) for b in bodies] == [True, False]


class Recorder:
    name = "recorder"
    supports_fast = True

    def __init__(self):
        self.fast = []

    def complete(self, *, system, messages, tools, max_tokens, on_text=None, fast=False):
        self.fast.append(fast)
        return LLMResponse(content=[{"type": "text", "text": "noted"}], stop_reason="end_turn")


def _api_agent(fast_when_chatting=True):
    config.load.cache_clear()
    s = config.load()
    s.llm.provider = "anthropic"
    s.llm.fast_when_chatting = fast_when_chatting
    s.sources.otter_logseq_graph = ""
    p = Recorder()
    return Agent(s, provider=p), p


def test_loop_is_fast_for_chat_and_voice_only():
    a, p = _api_agent()
    a.handle("hello", channel="app")                         # chat
    a.handle("hello", channel="voice", mode="voice")
    a.run_check("morning", "look at the day")                # scheduled check
    a.run_check("x", "y", kind="heartbeat")
    a.run_isolated("research this", "worker:1", "worker #1")
    assert p.fast == [True, True, False, False, False]


def test_setting_turns_it_off():
    a, p = _api_agent(fast_when_chatting=False)
    a.handle("hello", channel="app")
    assert p.fast == [False]


def test_sdk_turns_set_fast_mode_per_turn(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    s.llm.provider = "agent_sdk"
    s.sources.otter_logseq_graph = ""
    a = Agent(s)
    a.convo.set("sdk_api_fallback_until", None)
    seen = []

    async def fake_query(*, prompt, options=None, transport=None):
        await prompt.__anext__()
        seen.append(json.loads(options.settings))
        yield sdk.SystemMessage(subtype="init", data={"session_id": "s1", "fast_mode_state": "on" if seen[-1]["fastMode"] else "off"})
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="noted")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s1", result="noted", total_cost_usd=0)
    monkeypatch.setattr(sdk, "query", fake_query)
    a.handle("hello", channel="app")
    a.run_check("morning", "look at the day")
    assert seen == [{"fastMode": True}, {"fastMode": False}]
