"""The Claude Agent SDK as the engine (`llm.provider = "agent_sdk"`), with a scripted fake `query`."""
import asyncio
import threading
import json

import claude_agent_sdk as sdk

from ea import config
from ea.agent.core import Agent
from ea.llm.agent_sdk import PREFIX


def _agent(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    s.llm.provider = "agent_sdk"
    s.sources.otter_logseq_graph = ""
    a = Agent(s)
    a.convo.set("sdk_api_fallback_until", None)     # the session-wide test data dir keeps an earlier test's fallback window
    for k in ("sdk_session:main", "sdk_session:main:fast", "sdk_seen:main", "sdk_seen:main:fast"):   # ...and its sessions
        a.convo.set(k, None)
    return a, s


async def _first_text(prompt):
    """The runner streams its input: the first message is the user's text (plus any image blocks)."""
    if isinstance(prompt, str):
        return prompt
    first = await prompt.__anext__()
    return next(b["text"] for b in first["message"]["content"] if b.get("type") == "text")


def test_tools_are_wrapped_as_mcp_tools(monkeypatch):
    a, s = _agent(monkeypatch)
    assert a.provider.name == "agent_sdk" and a.sdk is not None
    spec = next(sp for sp in a.tools.specs() if sp.name == "list_reminders")
    wrapped = a.sdk._wrap(spec)
    assert wrapped.name == "list_reminders" and wrapped.input_schema.get("type") == "object"
    out = asyncio.run(wrapped.handler({}))
    assert out["content"][0]["type"] == "text" and "is_error" not in out
    bad = asyncio.run(a.sdk._wrap(next(sp for sp in a.tools.specs() if sp.name == "read_document")).handler({"name": "nope.md"}))
    assert "no document" in bad["content"][0]["text"]
    assert a.sdk.server()["type"] == "sdk" and a.sdk.server()["name"] == "ea"


def test_turn_is_recorded_streamed_and_session_resumed(monkeypatch):
    a, s = _agent(monkeypatch)
    seen = {"prompts": [], "options": []}

    async def fake_query(*, prompt, options=None, transport=None):
        seen["prompts"].append(await _first_text(prompt)); seen["options"].append(options)
        yield sdk.SystemMessage(subtype="init", data={"session_id": "sess-1"})
        yield sdk.AssistantMessage(content=[sdk.ThinkingBlock(thinking="I should check reminders.", signature="x"),
                                            sdk.ToolUseBlock(id="tu1", name=PREFIX + "list_reminders", input={})], model="m")
        yield sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="tu1", content=[{"type": "text", "text": "[]"}], is_error=False)])
        yield sdk.StreamEvent(uuid="u", session_id="sess-1", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Nothing "}})
        yield sdk.StreamEvent(uuid="u", session_id="sess-1", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "pending."}})
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Nothing pending.")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=2, session_id="sess-1", result="Nothing pending.", total_cost_usd=0.01)
    monkeypatch.setattr(sdk, "query", fake_query)
    events, activity = [], []
    a.activity = activity.append
    reply = a.handle("anything pending?", channel="app", on_event=events.append)
    assert reply == "Nothing pending."
    # the message carries the situation (volatile context) then the user's text; the system prompt is the stable
    # block only (a resumed session keeps it); our tools only
    o = seen["options"][0]
    first = seen["prompts"][0]
    assert first.startswith("# SITUATION") and "# NOW" in first and first.endswith("# THE MESSAGE\nanything pending?")
    assert "# NOW" not in o.system_prompt and o.tools == [] and o.permission_mode == "bypassPermissions"
    assert PREFIX + "list_reminders" in o.allowed_tools and o.resume is None and o.setting_sources == []
    assert o.thinking == {"type": "adaptive", "display": "summarized"} and o.effort == s.llm.effort
    # streamed text and tool events reached the caller; activity feed saw everything
    assert [e for e in events if e["type"] == "text"] == [{"type": "text", "text": "Nothing "}, {"type": "text", "text": "pending."}]
    assert [e["kind"] for e in activity] == ["turn", "thinking", "tool_call", "tool_result", "text"]
    assert activity[2]["name"] == "list_reminders" and activity[3]["name"] == "list_reminders" and activity[4]["final"] is True
    # the conversation store has the same Anthropic-shaped history, so switching back to the API later just works
    hist = a.convo.history(a.convo.current_thread(), 20)
    kinds = [(m["role"], [b["type"] for b in m["content"]]) for m in hist]
    assert kinds == [("user", ["text"]), ("assistant", ["thinking", "tool_use"]), ("user", ["tool_result"]), ("assistant", ["text"])]
    assert hist[1]["content"][1]["name"] == "list_reminders"              # prefix stripped
    # next turn resumes the SDK session
    a.handle("and now?", channel="app")
    assert seen["options"][1].resume == "sess-1"
    assert a.convo.readable(10)[-1]["text"] == "Nothing pending."


def test_sdk_error_is_reported_not_raised(monkeypatch):
    a, s = _agent(monkeypatch)

    async def fake_query(*, prompt, options=None, transport=None):
        yield sdk.ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=0, session_id="s", errors=["boom"])
    monkeypatch.setattr(sdk, "query", fake_query)
    assert a.handle("hi", channel="app").startswith("(model error: agent sdk")
    hist = a.convo.history(a.convo.current_thread(), 5)                  # the unanswered user message was removed
    assert not hist or hist[-1]["content"][0].get("text") != "hi"


def test_subscription_fallback_to_api(monkeypatch):
    """Near the end of the subscription window the SDK reports it; turns then run over the API until the reset,
    and the SDK session then resumes with a catch-up of what the API handled (it used to restart with a recap)."""
    import time
    from ea.llm.base import LLMResponse
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    a, s = _agent(monkeypatch)
    assert a.api_provider is not None
    api_calls = []

    class FakeAPI:
        name = "anthropic"
        def complete(self, **kw):
            api_calls.append(kw)
            return LLMResponse(content=[{"type": "text", "text": "api reply"}], stop_reason="end_turn")
    a.api_provider = FakeAPI()
    sdk_prompts = []
    reset = int(time.time()) + 600

    async def fake_query(*, prompt, options=None, transport=None):
        sdk_prompts.append((await _first_text(prompt), options))
        yield sdk.RateLimitEvent(rate_limit_info=sdk.RateLimitInfo(status="allowed_warning", resets_at=reset, rate_limit_type="five_hour", utilization=0.93), uuid="u", session_id="s1")
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="sdk reply")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s1", result="sdk reply")
    monkeypatch.setattr(sdk, "query", fake_query)
    assert a.handle("one", channel="app") == "sdk reply"                 # this turn still completes on the subscription
    assert a.sdk.describe()["billing"].startswith("api") and a.sdk.describe()["subscription_utilization"] == 0.93
    assert a.handle("two", channel="app") == "api reply"                 # fallback: over the API, with the stored history
    assert len(sdk_prompts) == 1 and len(api_calls) == 1
    assert [m["role"] for m in api_calls[0]["messages"]][:3] == ["user", "assistant", "user"]
    # the window resets: back to the subscription, same session, told what the API handled meanwhile
    a.convo.set("sdk_api_fallback_until", time.time() - 1)
    assert a.handle("three", channel="app") == "sdk reply"
    text, o = sdk_prompts[1]
    assert o.resume == "s1" and "RECENT CONVERSATION" not in o.system_prompt
    assert "# WHAT HAPPENED OUTSIDE THIS THREAD" in text and "\ntwo" in text and "api reply" in text and text.endswith("# THE MESSAGE\nthree")
    assert sdk_prompts[1][1].env == {"ANTHROPIC_API_KEY": ""}            # the subprocess never sees the API key


def test_rejected_limit_redoes_turn_over_api(monkeypatch):
    from ea.llm.base import LLMResponse
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    a, s = _agent(monkeypatch)

    class FakeAPI:
        name = "anthropic"
        def complete(self, **kw):
            return LLMResponse(content=[{"type": "text", "text": "api reply"}], stop_reason="end_turn")
    a.api_provider = FakeAPI()

    async def fake_query(*, prompt, options=None, transport=None):
        yield sdk.RateLimitEvent(rate_limit_info=sdk.RateLimitInfo(status="rejected", resets_at=None, rate_limit_type="five_hour", utilization=1.0), uuid="u", session_id="s1")
    monkeypatch.setattr(sdk, "query", fake_query)
    assert a.handle("hi", channel="app") == "api reply"
    assert not a.sdk.use_subscription_now()


def test_usage_is_tracked_and_user_warned_once_per_window(monkeypatch):
    import time
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    a, s = _agent(monkeypatch)
    told = []
    a.sdk.notify = told.append
    for k in ("sdk_api_fallback_until", "sdk_warned_for", "sdk_rate_limit_log"):   # shared store across tests
        a.convo.set(k, None)
    reset = int(time.time()) + 3600
    a.sdk.note_rate_limit(sdk.RateLimitInfo(status="allowed", resets_at=reset, rate_limit_type="five_hour", utilization=0.5))
    assert told == [] and a.sdk.prompt_line().startswith("Running on the Claude subscription: five_hour 50% used")
    a.sdk.note_rate_limit(sdk.RateLimitInfo(status="allowed", resets_at=reset, rate_limit_type="five_hour", utilization=0.8))
    a.sdk.note_rate_limit(sdk.RateLimitInfo(status="allowed", resets_at=reset, rate_limit_type="five_hour", utilization=0.82))
    assert len(told) == 1 and "80% used" in told[0] and a.sdk.use_subscription_now()
    a.sdk.note_rate_limit(sdk.RateLimitInfo(status="allowed", resets_at=reset, rate_limit_type="five_hour", utilization=0.95))
    assert len(told) == 2 and "running on the API key" in told[1] and not a.sdk.use_subscription_now()
    assert [r["utilization"] for r in a.sdk.readings()][-4:] == [0.5, 0.8, 0.82, 0.95]
    assert a.sdk.prompt_line().startswith("Running on the API key")
    from ea import integrations
    eng = next(r for r in integrations.status(s) if r["key"] == "engine")
    assert eng["state"] == "partial" and "api" in eng["detail"]


def test_message_sent_mid_turn_reaches_the_running_turn(monkeypatch):
    """Eli, 2026-11-11: on the SDK engine a message typed while the agent worked only started the next turn, in a
    batch. Now the runner polls the inbox and writes it to the CLI's stdin as a further user message."""
    a, s = _agent(monkeypatch)
    seen = {"injected": []}

    async def fake_query(*, prompt, options=None, transport=None):
        first = await prompt.__anext__()
        yield sdk.SystemMessage(subtype="init", data={"session_id": "sess-2"})
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="tu1", name=PREFIX + "list_reminders", input={})], model="m")
        assert a.submit("also check the calendar", channel="app", client_id="phone") is True     # typed meanwhile
        nxt = await prompt.__anext__()                                   # the runner hands it over before the turn ends
        seen["injected"].append(nxt["message"]["content"][-1]["text"])
        yield sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="tu1", content=[{"type": "text", "text": "[]"}], is_error=False)])
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Nothing pending, calendar clear.")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=2, session_id="sess-2",
                                result="Nothing pending, calendar clear.", total_cost_usd=0.01)
        try:
            await prompt.__anext__()
        except StopAsyncIteration:
            pass                                                        # the stream ends after the result
    monkeypatch.setattr(sdk, "query", fake_query)
    said = []
    a.on_message = said.append
    reply = a.handle("anything pending?", channel="app")
    assert reply == "Nothing pending, calendar clear."
    from ea.agent.conversations import MID_TURN_PREFIX
    assert seen["injected"] == [MID_TURN_PREFIX + "also check the calendar"]
    # recorded in the conversation (prefix stripped for readers) and announced to the other clients, once
    assert [m["text"] for m in a.convo.readable(10) if m["who"] == "me"][-2:] == ["anything pending?", "also check the calendar"]
    assert [m["text"] for m in said if m["who"] == "me"] == ["anything pending?", "also check the calendar"]
    assert a._inbox == []                                               # nothing left for a "late" next turn


def test_tool_dispatch_threads_carry_the_turns_context(monkeypatch):
    """Tools run on pool threads under the SDK; each call is tagged with whose turn it is (main or a worker), which
    the browser uses to keep one Chromium per context."""
    from ea.connectors.browser import current_context
    a, s = _agent(monkeypatch)
    seen = []
    a.tools.tool("ctx_probe", "test", {"type": "object", "properties": {}})(lambda: seen.append((threading.current_thread().name, current_context())) or "ok")
    wrapped = a.sdk._wrap(next(sp for sp in a.tools.specs() if sp.name == "ctx_probe"))
    a.sdk._context = "worker:7"
    asyncio.run(wrapped.handler({}))
    a.sdk._context = "main"
    asyncio.run(wrapped.handler({}))
    assert [c for _, c in seen] == ["worker:7", "main"]
    assert current_context() == "main"                      # the tag never leaks past the call


def test_changed_standing_prompt_restarts_the_session_with_a_recap(monkeypatch):
    """A resumed Claude Code session ignores a changed system prompt (verified 2026-11-12), so when the stable
    block changes (code, standing instructions) the next turn starts a fresh session and recaps."""
    from ea.agent import prompt as prompt_mod
    a, s = _agent(monkeypatch)
    seen = []

    async def fake_query(*, prompt, options=None, transport=None):
        seen.append((await _first_text(prompt), options))
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="noted, resumed")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="sess-9", result="noted, resumed")
    monkeypatch.setattr(sdk, "query", fake_query)
    a.handle("one", channel="app")
    a.handle("two", channel="app")
    assert seen[1][1].resume == "sess-9"                                  # same instructions: resumed
    stable = prompt_mod.stable_block
    monkeypatch.setattr(prompt_mod, "stable_block", lambda settings: stable(settings) + "\n\nA new standing rule.")
    a.handle("three", channel="app")
    assert seen[2][1].resume is None and "instructions changed" in seen[2][1].system_prompt and "one" in seen[2][1].system_prompt
    a.handle("four", channel="app")
    assert seen[3][1].resume == "sess-9"                                  # and resumes again from there


def test_message_sent_during_a_background_turn_still_shows_in_the_chat(monkeypatch):
    """Eli, 2026-11-16: a message typed while a heartbeat ran reached the agent but was stored under the heartbeat's
    channel ('scheduler'), which the chat history hides, so it vanished from the app and looked unsent."""
    import threading
    a, s = _agent(monkeypatch)
    stored = threading.Event()

    async def fake_query(*, prompt, options=None, transport=None):
        await prompt.__anext__()
        yield sdk.SystemMessage(subtype="init", data={"session_id": "sess-3"})
        yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="tu1", name=PREFIX + "list_reminders", input={})], model="m")
        assert a.submit("whiteboard please (sdk)", channel="app", client_id="mac", stored=stored) is True
        assert not stored.is_set()                                       # not in the table yet: the app must not be told "done"
        await prompt.__anext__()
        assert stored.is_set()
        yield sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="tu1", content=[{"type": "text", "text": "[]"}], is_error=False)])
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Done.")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=10, duration_api_ms=8, is_error=False, num_turns=2, session_id="sess-3",
                                result="Done.", total_cost_usd=0.01)
        try:
            await prompt.__anext__()
        except StopAsyncIteration:
            pass
    monkeypatch.setattr(sdk, "query", fake_query)
    a.run_check("heartbeat", "look around", kind="heartbeat")
    mine = [m for m in a.convo.readable(10) if m["who"] == "me"][-1:]   # the last one: the store is shared between tests
    assert [(m["text"], m["channel"]) for m in mine] == [("whiteboard please (sdk)", "app")]   # the heartbeat's own prompt stays hidden
