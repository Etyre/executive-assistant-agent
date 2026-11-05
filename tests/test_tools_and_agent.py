"""The agent loop with a fake provider and fake services: proves tool dispatch, tool_result pairing,
memory dating through the tools, schedule-setting payload, and end_conversation behaviour."""
import copy
import datetime as dt
import json
import threading

import httpx
import pytest

from ea import config
from ea.agent.core import Agent
from ea.connectors.http import ServiceRouter
from ea.llm.base import LLMResponse, ToolSpec
from ea.tools.registry import ToolRegistry


class ScriptedProvider:
    name = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, *, system, messages, tools, max_tokens):
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools})
        return self.script.pop(0)


def text(t):
    return LLMResponse(content=[{"type": "text", "text": t}], stop_reason="end_turn")


def tool(name, inp, tid="u1"):
    return LLMResponse(content=[{"type": "tool_use", "id": tid, "name": name, "input": inp}], stop_reason="tool_use")


class FakeTracker:
    """A stand-in for the Project Tracker reached through the router (httpx MockTransport)."""

    def __init__(self):
        self.received = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.received.append((request.method, request.url.path, request.content.decode()))
        self.headers = dict(request.headers)
        if request.url.path == "/api/agent/schedule" and request.method == "PUT":
            body = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "dropped": [], "plan": {"date": body.get("date") or "today", "items": body["items"],
                                                                                "raw": {"pinned": body["pin"], "source": "agent:Atlas"}}})
        if request.url.path == "/api/agent/tree":
            return httpx.Response(200, json={"projects": [{"id": 1, "path": ["Work"]}], "tree": [
                {"id": 1, "name": "Work", "children": [], "notes_added": [], "tasks": [
                    {"id": 7, "title": "Write memo", "deadline": "2026-11-06", "importance": 2, "blocking": 0, "children": [],
                     "notes_added": [{"author": "Atlas", "body": "Sam wants it Friday"}]}]}]})
        return httpx.Response(404)


class FakeRouter(ServiceRouter):
    def __init__(self, handler):
        self.handler = handler
        self.bridge = ""

    def client(self, service):
        return httpx.Client(transport=httpx.MockTransport(self.handler), base_url=f"http://{service}")

    def describe(self):
        return "fake"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", config.ROOT / "instructions")
    config.load.cache_clear()
    s = config.load()
    s.timezone = "UTC"
    tracker = FakeTracker()
    a = Agent(s, provider=ScriptedProvider([]), router=FakeRouter(tracker.handler))
    a.fake_tracker = tracker
    return a


def test_registry_dispatch_errors_are_text():
    reg = ToolRegistry()

    @reg.tool("boom", "explodes", {"properties": {"x": {"type": "integer"}}, "required": ["x"]})
    def boom(x: int):
        raise ValueError("nope")

    out, err = reg.dispatch("boom", {"x": 1})
    assert err and out == "ValueError: nope"
    out, err = reg.dispatch("boom", {})
    assert err and "bad arguments" in out
    out, err = reg.dispatch("missing", {})
    assert err and "unknown tool" in out
    assert isinstance(reg.specs()[0], ToolSpec)


def test_turn_with_tool_call_and_memory_dating(agent):
    agent.provider.script = [
        tool("memory_write", {"title": "Bedtime policy", "body": "Lights out 23:00.", "tags": ["policy"], "conversation_date": "2026-10-30"}),
        text("Saved."),
    ]
    reply = agent.handle("remember: lights out at 23:00, we agreed last week", channel="telegram")
    assert reply == "Saved."
    m = agent.memory.read("bedtime-policy")
    assert m["sources"] == ["2026-10-30 telegram"] and m["edits"] == [dt.date.today().isoformat()]
    # the second model call saw the tool_result paired with the tool_use
    second = agent.provider.calls[1]["messages"]
    assert second[-3]["content"][0]["type"] == "tool_use" and second[-2]["content"][0]["tool_use_id"] == "u1"
    assert second[-2]["content"][0]["is_error"] is False
    # the system prompt carries the user's instructions and the memory index
    sys_text = "\n".join(b["text"] for b in agent.provider.calls[0]["system"])
    assert "executive assistant" in sys_text and "Never, ever, compliment me" in sys_text
    assert agent.provider.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    # the volatile block rides at the tail as a system message, rebuilt per call: the new memory shows up mid-turn
    tail0, tail1 = agent.provider.calls[0]["messages"][-1], agent.provider.calls[1]["messages"][-1]
    assert tail0["role"] == "system" and "# MEMORY INDEX" in tail0["content"] and "bedtime-policy" not in tail0["content"]
    assert tail1["role"] == "system" and "bedtime-policy" in tail1["content"]


def test_set_schedule_goes_to_tracker_as_pinned_put(agent):
    items = [{"kind": "task", "task_id": 7, "title": "Write memo", "start": "09:00", "end": "10:30"},
             {"kind": "break", "title": "Break", "start": "10:30", "end": "10:40"}]
    agent.provider.script = [
        tool("tracker_outline", {}, "a"),
        tool("tracker_set_schedule", {"items": items, "work_start": "09:00", "work_end": "17:00", "rationale": "memo first"}, "b"),
        text("Schedule set."),
    ]
    assert agent.handle("set today's schedule: memo first, then a break", channel="web") == "Schedule set."
    puts = [r for r in agent.fake_tracker.received if r[0] == "PUT"]
    assert len(puts) == 1
    body = json.loads(puts[0][2])
    assert body["items"] == items and body["pin"] is True and puts[0][1] == "/api/agent/schedule"
    assert agent.fake_tracker.headers["x-agent-name"] == "Atlas"            # attributed to the agent by name
    # the outline tool rendered the tree as text with ids the model can quote, notes included
    outline_result = agent.provider.calls[1]["messages"][-2]["content"][0]["content"]
    assert "* Work (project 1)" in outline_result and "[7] Write memo due 2026-11-06 p2" in outline_result
    assert "note [Atlas]: Sam wants it Friday" in outline_result


def test_proactive_check_uses_send_message_or_not(agent):
    sent = []
    agent.outbound = sent.append
    agent.provider.script = [tool("no_action_needed", {"reason": "all fine"}), text("nothing to report")]
    agent.run_check("morning", "look at things")
    assert sent == []
    agent.provider.script = [tool("send_message", {"text": "Your HRV is down 20% - want to talk about tonight?"}), text("sent")]
    agent.run_check("evening", "look again")
    assert sent == ["Your HRV is down 20% - want to talk about tonight?"]
    first_user = agent.provider.calls[0]["messages"][0]["content"][0]["text"]
    assert first_user.startswith("[scheduled check: morning]")


def test_reminder_tool_and_scheduler_delivery(agent):
    from ea.scheduler.jobs import Scheduler
    sent = []
    agent.outbound = sent.append
    past = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    agent.provider.script = [tool("set_reminder", {"when": past, "text": "Call the dentist"}), text("ok")]
    agent.handle("remind me", channel="cli")
    Scheduler(agent, agent.settings).tick()
    assert sent == ["Call the dentist"]
    assert agent.convo.pending_reminders() == []


def test_end_conversation_starts_new_thread(agent):
    agent.provider.script = [tool("end_conversation", {"reason": "not willing"}), text("I'm ending this here.")]
    assert agent.handle("...", channel="telegram") == "I'm ending this here."
    assert agent.convo.current_thread().startswith("main@")
    agent.provider.script = [text("Fresh start.")]
    agent.handle("ok, new topic", channel="telegram")
    first = agent.provider.calls[-1]["messages"][0]["content"][0]["text"]
    assert first.startswith("[system note: you ended the previous conversation")
    assert len(agent.provider.calls[-1]["messages"]) == 2          # the new user turn + volatile system; old thread gone


def test_llm_error_is_returned_not_raised(agent):
    from ea.llm.base import LLMError

    class Failing:
        name = "failing"

        def complete(self, **kw):
            raise LLMError("rate limited")

    agent.provider = Failing()
    assert agent.handle("hi", channel="cli").startswith("(model error: rate limited)")


def test_concurrent_turns_are_serialised(agent):
    agent.provider.script = [text("one"), text("two")]
    results = []
    ts = [threading.Thread(target=lambda: results.append(agent.handle("x", channel="cli"))) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["one", "two"]
