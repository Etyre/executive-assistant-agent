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

    def complete(self, *, system, messages, tools, max_tokens, on_text=None):
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools})
        resp = self.script.pop(0)
        if on_text and resp.text:                      # stream the text in two pieces like a real provider would
            on_text(resp.text[:3]); on_text(resp.text[3:])
        return resp


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


def test_failed_turn_leaves_no_dangling_prompt(agent):
    """A scheduled check whose model call fails (e.g. no API key) must not leave its prompt in the history:
    seen 2026-11-04, when the next chat message was answered as if it were the evening check."""
    from ea.llm.base import LLMError

    class Failing:
        name = "failing"

        def __init__(self, exc):
            self.exc = exc

        def complete(self, **kw):
            raise self.exc

    thread = agent.convo.current_thread()
    before = len(agent.convo.history(thread))
    agent.provider = Failing(LLMError("rate limited"))
    agent.run_check("evening", "look at things")
    assert len(agent.convo.history(thread)) == before
    agent.provider = Failing(TypeError("Could not resolve authentication method"))
    import pytest
    with pytest.raises(TypeError):
        agent.run_check("evening", "look at things")
    assert len(agent.convo.history(thread)) == before


def test_streamed_turn_reports_tools_and_text_as_it_goes(agent):
    agent.provider.script = [tool("tracker_feed", {}), text("Right now: planning block.")]
    events = []
    reply = agent.handle("what's on?", channel="web", on_event=events.append)
    assert reply == "Right now: planning block."
    assert events[0] == {"type": "tool", "name": "tracker_feed"}
    assert "".join(e["text"] for e in events if e["type"] == "text") == reply
    # without a listener nothing changes
    agent.provider.script = [text("ok")]
    assert agent.handle("thanks", channel="web") == "ok"


def test_daily_checklist_is_read_only(agent):
    """Eli (2026-11-04): the agent reads the daily checklist but never ticks it."""
    assert "tracker_checklist" in agent.tools
    assert "tracker_checklist_done" not in agent.tools
    assert not any("checklist" in n and agent.tools._tools[n].writes for n in agent.tools.names())
    assert not hasattr(agent.tracker, "checklist_done")


def test_document_roots_are_listed_and_read_live(tmp_path, monkeypatch):
    """Eli, 2026-11-06: the agent must always have the current version of his site's source docs."""
    from ea import config
    from ea.agent import prompt
    from ea.tools import assistant_tools
    from ea.tools.registry import ToolRegistry
    from ea.agent.conversations import ConversationStore
    config.load.cache_clear()
    s = config.load()
    site = tmp_path / "source_docs"; site.mkdir(); (site / "images").mkdir()
    (site / "Sleep.md").write_text("# Sleep\nIn bed by 00:30.")
    (site / "images" / "x.md").write_text("ignored? no, listed")
    s.document_roots = [config.DocumentRoot(name="elityre.com", path=str(site), note="the site sources")]
    reg = ToolRegistry()
    store = ConversationStore(tmp_path / "s.db", tmp_path / "t")
    assistant_tools.register(reg, store, s, lambda t: None, lambda r: None)
    names = {d["name"] for d in json.loads(reg.dispatch("list_documents", {})[0])}
    assert "elityre.com/Sleep.md" in names and "STANDING_INSTRUCTIONS.md" in names
    assert reg.dispatch("read_document", {"name": "elityre.com/Sleep.md"})[0].endswith("In bed by 00:30.")
    (site / "Sleep.md").write_text("# Sleep\nIn bed by 23:30.")                   # edited: next read sees it
    assert reg.dispatch("read_document", {"name": "elityre.com/Sleep.md"})[0].endswith("In bed by 23:30.")
    assert reg.dispatch("read_document", {"name": "elityre.com/../secret.md"})[0] == "outside the documents folder"
    assert "no document" in reg.dispatch("read_document", {"name": "elityre.com/nope.md"})[0]
    from ea.memory.store import MemoryStore
    block = prompt.volatile_block(s, MemoryStore(tmp_path / "mem"), store, "app", "chat")
    assert "# DOCUMENT ROOTS" in block and "elityre.com/ (2 files" in block and "the site sources" in block


def test_message_sent_mid_turn_is_folded_into_the_running_turn(monkeypatch):
    """Eli, 2026-11-06: sending during the agent's turn. The API engine hands it to the model at the next step; a
    message that lands after the model finished starts the next turn on its own."""
    from ea import config
    from ea.agent.core import Agent
    from ea.llm.base import LLMResponse
    config.load.cache_clear()
    s = config.load()
    s.sources.otter_logseq_graph = ""
    calls, said = [], []

    class P:
        name = "scripted"
        def __init__(self): self.agent = None
        def complete(self, system, messages, tools, max_tokens, **kw):
            calls.append(messages)
            n = len(calls)
            if n == 1:                                   # first step: call a tool; meanwhile the user types
                assert self.agent.submit("also, make it short", "app", "mac") is True
                return LLMResponse(content=[{"type": "tool_use", "id": "t1", "name": "list_reminders", "input": {}}], stop_reason="tool_use")
            if n == 2:                                   # the model finishes; the user sends again just after
                self.agent.submit("and one more thing", "app", "mac")
                return LLMResponse(content=[{"type": "text", "text": "first reply"}], stop_reason="end_turn")
            return LLMResponse(content=[{"type": "text", "text": "follow-up reply"}], stop_reason="end_turn")
    p = P()
    a = Agent(s, provider=p); p.agent = a
    a.on_message = said.append
    assert a.submit("x", "app", "mac") is False                      # nothing running: the caller handles it normally
    assert a.handle("do the thing", channel="app", client_id="mac") == "first reply"
    # step 2 saw the tool result AND the mid-turn message in one user message
    last_user = calls[1][-2]                                           # [-1] is the trailing volatile system message
    assert last_user["role"] == "user" and [b["type"] for b in last_user["content"]] == ["tool_result", "text"]
    assert "also, make it short" in last_user["content"][1]["text"]
    # the late message started a follow-up turn of its own, and both replies went out on the live feed
    assert len(calls) == 3 and "and one more thing" in calls[2][-2]["content"][0]["text"]
    assert [(m["who"], m["text"]) for m in said] == [("me", "do the thing"), ("me", "also, make it short"), ("it", "first reply"),
                                                     ("me", "and one more thing"), ("it", "follow-up reply")]
    assert [m["text"] for m in a.convo.readable(10)][-5:] == ["do the thing", "also, make it short", "first reply", "and one more thing", "follow-up reply"]
