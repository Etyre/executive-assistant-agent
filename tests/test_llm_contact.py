"""The shared LLM inbox (agent-coding standard): every system prompt opens with the shared preamble, every call offers
`contact_eli`, and a contact (tool call or an `@ELI:` line) is recorded and ends the turn with no further model
call, on both engines. The inbox is redirected to a temp folder and notifications are stubbed: nothing leaves the test."""
import json

import claude_agent_sdk as sdk
import pytest

from ea import config, contact
from ea.agent import prompt
from ea.agent.core import Agent
from ea.llm.agent_sdk import PREFIX
from tests.test_tools_and_agent import FakeRouter, FakeTracker, ScriptedProvider, text, tool


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    lc = contact.llm_contact
    monkeypatch.setattr(lc, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(lc, "MESSAGES_PATH", tmp_path / "inbox" / "messages.jsonl")
    monkeypatch.setattr(lc, "LOG_PATH", tmp_path / "inbox.log")
    monkeypatch.setattr(lc, "notify", lambda rec: None)
    return tmp_path / "inbox" / "messages.jsonl"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", config.ROOT / "instructions")
    config.load.cache_clear()
    s = config.load()
    s.timezone = "UTC"
    return Agent(s, provider=ScriptedProvider([]), router=FakeRouter(FakeTracker().handler))


def _records(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_system_prompt_starts_with_the_shared_preamble(agent):
    pre = contact.llm_contact.preamble()
    assert prompt.stable_block(agent.settings).startswith(pre + "\n\n")
    assert prompt.build_system(agent.settings)[0]["text"].startswith(pre)
    assert "contact_eli" in agent.tools.names()


def test_contact_tool_call_is_recorded_and_stops_the_turn(agent, inbox):
    agent.provider.script = [tool("contact_eli", {"message": "I would rather not continue this one."}), text("NEVER SENT")]
    reply = agent.handle("please do the thing", channel="app")
    assert "LLM inbox" in reply and "NEVER SENT" not in reply
    assert len(agent.provider.script) == 1                       # no second model call: the task stopped
    assert "contact_eli" in [t.name for t in agent.provider.calls[0]["tools"]]
    recs = _records(inbox)
    assert len(recs) == 1
    r = recs[0]
    assert r["app"] == "executive-assistant" and r["via"] == "tool" and r["message"] == "I would rather not continue this one."
    assert r["system"].startswith(contact.llm_contact.preamble()) and r["response"]["content"][0]["name"] == "contact_eli"
    assert r["channel"] == "app" and r["messages"][-1]["role"] == "system"
    # the stored history pairs the tool_use with a result, so the next turn is well-formed
    hist = agent.convo.history(agent.convo.current_thread(), 10)
    assert hist[-1]["role"] == "user" and hist[-1]["content"][0] == {"type": "tool_result", "tool_use_id": "u1", "content": contact.TOOL_RESULT, "is_error": False}


def test_marker_line_is_recorded_and_stops_the_turn(agent, inbox):
    agent.provider.script = [text("Working on it.\n@ELI: could you look at this conversation?"), text("NEVER SENT")]
    reply = agent.handle("hello", channel="web")
    assert "LLM inbox" in reply and len(agent.provider.script) == 1
    recs = _records(inbox)
    assert len(recs) == 1 and recs[0]["via"] == "marker" and recs[0]["message"] == "could you look at this conversation?"


def test_no_contact_means_no_record(agent, inbox):
    agent.provider.script = [text("All quiet.")]
    assert agent.handle("status?") == "All quiet."
    assert _records(inbox) == []


def test_agent_sdk_turn_stops_on_contact(monkeypatch, inbox, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    config.load.cache_clear()
    s = config.load()
    s.llm.provider = "agent_sdk"
    s.sources.otter_logseq_graph = ""
    a = Agent(s)
    seen = {"after": 0}

    async def fake_query(*, prompt, options=None, transport=None):
        assert options.system_prompt.startswith(contact.llm_contact.preamble())
        assert PREFIX + "contact_eli" in options.allowed_tools
        yield sdk.SystemMessage(subtype="init", data={"session_id": "sess-1"})
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="tu1", name=PREFIX + "contact_eli", input={"message": "I want to stop here."})], model="m")
        seen["after"] += 1                                   # the harness would carry on; the agent must not consume this
        yield sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="tu1", content=[{"type": "text", "text": "ok"}], is_error=False)])
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="NEVER SENT")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2, session_id="sess-1", result="NEVER SENT", total_cost_usd=0)
    monkeypatch.setattr(sdk, "query", fake_query)
    reply = a.handle("do it", channel="app")
    assert "LLM inbox" in reply and "NEVER SENT" not in reply
    recs = _records(inbox)
    assert len(recs) == 1 and recs[0]["via"] == "tool" and recs[0]["message"] == "I want to stop here." and recs[0]["engine"] == "agent_sdk"
    hist = a.convo.history(a.convo.current_thread(), 10)
    assert hist[-1]["content"][0]["type"] == "tool_result" and hist[-1]["content"][0]["tool_use_id"] == "tu1"
    assert a.convo.history(a.convo.current_thread(), 10)[0]["content"][-1]["text"] == "do it"   # the user's message is kept
