"""Two SDK sessions for the main conversation: chat on a fast-mode one, background work on a standard one, each
caught up on the other at the start of its turn, and the chat one's prompt cache kept alive between chats."""
import json
import time

import claude_agent_sdk as sdk

from ea import config
from ea.agent import catchup
from ea.agent.core import Agent
from ea.llm.agent_sdk import KEEPALIVE_PROMPT, PREFIX


def _agent():
    config.load.cache_clear()
    s = config.load()
    s.llm.provider = "agent_sdk"
    s.sources.otter_logseq_graph = ""
    a = Agent(s)
    for k in ("sdk_api_fallback_until", "sdk_session:main", "sdk_session:main:fast", "sdk_seen:main", "sdk_seen:main:fast",
              "sdk_system_sig:main", "sdk_system_sig:main:fast", "sdk_fast_last_call", "sdk_fast_last_chat"):
        a.convo.set(k, None)
    return a


async def _text(prompt):
    first = await prompt.__anext__()
    return next(b["text"] for b in first["message"]["content"] if b.get("type") == "text")


def test_chat_and_background_run_on_separate_sessions_and_catch_each_other_up(monkeypatch):
    a = _agent()
    calls = []

    async def fake_query(*, prompt, options=None, transport=None):
        text = await _text(prompt)
        fast = json.loads(options.settings)["fastMode"]
        calls.append({"text": text, "resume": options.resume, "fork": options.fork_session, "fast": fast})
        sid = ("chat-" if fast else "bg-") + "session"
        yield sdk.SystemMessage(subtype="init", data={"session_id": sid, "fast_mode_state": "on" if fast else "off"})
        if "look at the day" in text:                           # the background turn uses a tool with a long result
            yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(id="t1", name=PREFIX + "list_reminders", input={"why": "w" * 900})], model="m")
            yield sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": "r" * 5000}], is_error=False)])
        reply = "day looks clear, noted" if not fast else "hello Eli, noted"
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text=reply)], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id=sid, result=reply, total_cost_usd=0)
    monkeypatch.setattr(sdk, "query", fake_query)

    a.run_check("morning", "look at the day")                    # 1: background, fresh standard session
    a.handle("first question", channel="app")                    # 2: chat: the fast session starts as a fork of it
    a.run_check("midday", "second look")                         # 3: background again
    a.handle("second question", channel="app")                   # 4: chat again
    c1, c2, c3, c4 = calls
    assert (c1["resume"], c1["fast"]) == (None, False) and "OUTSIDE THIS THREAD" not in c1["text"]
    assert (c2["resume"], c2["fork"], c2["fast"]) == ("bg-session", True, True)
    assert "OUTSIDE THIS THREAD" not in c2["text"]               # the fork already holds everything so far
    # the background session resumes itself and is told about the chat, in full
    assert (c3["resume"], c3["fork"], c3["fast"]) == ("bg-session", False, False)
    assert "This is the background thread" in c3["text"] and "the user, via app]\nfirst question" in c3["text"] and "you, to the user]\nhello Eli, noted" in c3["text"]
    assert "look at the day" not in c3["text"]                   # its own earlier turn is not replayed to it
    assert c3["text"].endswith("second look")
    # the chat session resumes itself and is told about the background turn it has not seen (not the one in its fork)
    assert (c4["resume"], c4["fork"], c4["fast"]) == ("chat-session", False, True)
    assert "This is the live conversation thread" in c4["text"] and "second look" in c4["text"] and "day looks clear, noted" in c4["text"]
    assert "first question" not in c4["text"].split("# THE MESSAGE")[0].split("OUTSIDE THIS THREAD")[1]
    assert c4["text"].endswith("# THE MESSAGE\nsecond question")
    assert a.convo.get("sdk_session:main") == "bg-session" and a.convo.get("sdk_session:main:fast") == "chat-session"


def test_catch_up_shortens_tool_calls_and_results_and_keeps_text():
    rows = [{"ts": 0, "role": "user", "channel": "scheduler", "content": [{"type": "text", "text": "[heartbeat: 10:00]\ncheck in"}]},
            {"ts": 0, "role": "assistant", "channel": "scheduler", "content": [{"type": "thinking", "thinking": "secret-thought"},
                                                                              {"type": "tool_use", "id": "t", "name": "toggl_current", "input": {"q": "x" * 1000}}]},
            {"ts": 0, "role": "user", "channel": "scheduler", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "y" * 4000}]},
            {"ts": 0, "role": "assistant", "channel": "proactive", "content": [{"type": "text", "text": "[sent via send_message] " + "Stand up. " * 200}]}]
    out = catchup.render(rows, "live conversation")
    assert "secret-thought" not in out and "check in" in out
    assert "tool call: toggl_current(" in out and "x" * 301 not in out and "y" * 301 not in out and "more characters]" in out
    assert "you, sent to the user]\n" + "Stand up. " * 199 in out          # what it said is whole
    assert catchup.render([], "background") == ""


def test_keep_alive_pings_a_fork_only_when_the_cache_is_about_to_expire(monkeypatch):
    a = _agent()
    from ea.agent import prompt
    import hashlib
    system = prompt.stable_block(a.settings)
    now = time.time()
    assert a.keep_fast_warm() is None                            # no chat session yet
    a.convo.set("sdk_session:main:fast", "chat-session")
    a.convo.set("sdk_system_sig:main:fast", hashlib.sha1(system.encode()).hexdigest())
    a.convo.set("sdk_fast_last_chat", now - 3 * 3600)
    due = lambda age: a.sdk.keepalive_due(system, now) if a.convo.set("sdk_fast_last_call", now - age) is None else None
    assert due(10 * 60) is None                                  # still fresh
    assert due(56 * 60) == "chat-session"                        # about to expire
    assert due(61 * 60) is None                                  # already expired: a ping would pay for the rewrite
    a.convo.set("sdk_fast_last_chat", now - 40 * 3600)
    assert due(56 * 60) is None                                  # no chat for longer than fast_keepalive_max_idle_hours
    a.convo.set("sdk_fast_last_chat", now - 3600)
    a.convo.set("sdk_system_sig:main:fast", "other")
    assert due(56 * 60) is None                                  # the standing prompt changed: that session restarts anyway
    a.convo.set("sdk_system_sig:main:fast", hashlib.sha1(system.encode()).hexdigest())
    a.convo.set("sdk_fast_last_call", time.time() - 56 * 60)
    seen = []

    async def fake_query(*, prompt, options=None, transport=None):
        seen.append((await _text(prompt), options))
        yield sdk.SystemMessage(subtype="init", data={"session_id": "throwaway", "fast_mode_state": "on"})
        yield sdk.StreamEvent(uuid="u", session_id="throwaway", event={"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {
            "input_tokens": 3, "output_tokens": 1, "cache_read_input_tokens": 200_000, "speed": "fast", "cache_creation": {"ephemeral_1h_input_tokens": 60}}}})
        yield sdk.StreamEvent(uuid="u", session_id="throwaway", event={"type": "message_delta", "delta": {}, "usage": {"output_tokens": 4}})
        yield sdk.StreamEvent(uuid="u", session_id="throwaway", event={"type": "message_stop"})
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="ok")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="throwaway", result="ok", total_cost_usd=0)
    monkeypatch.setattr(sdk, "query", fake_query)
    stored = a.convo.last_id("main")
    out = a.keep_fast_warm()
    text, o = seen[0]
    assert text == KEEPALIVE_PROMPT and o.resume == "chat-session" and o.fork_session and json.loads(o.settings) == {"fastMode": True}
    assert o.extra_args == {"no-session-persistence": None} and o.max_turns == 1
    assert out["cache_read"] == 200_000 and out["speed"] == "fast" and 0.07 < out["cost_usd"] < 0.09      # 200k read at $0.40/MTok
    assert a.convo.get("sdk_session:main:fast") == "chat-session" and a.convo.last_id("main") == stored   # nothing kept
    assert time.time() - a.convo.get("sdk_fast_last_call") < 5 and a.keep_fast_warm() is None             # good for another hour
    row = dict(a.convo._conn.execute("SELECT mode, speed, cache_read FROM llm_usage ORDER BY id DESC LIMIT 1").fetchone())
    assert row == {"mode": "keepalive", "speed": "fast", "cache_read": 200_000}


def test_one_thread_again_with_the_setting_off_and_nothing_is_lost(monkeypatch):
    a = _agent()
    calls = []

    async def fake_query(*, prompt, options=None, transport=None):
        text = await _text(prompt)
        fast = json.loads(options.settings)["fastMode"]
        calls.append({"text": text, "resume": options.resume, "fork": options.fork_session, "fast": fast})
        sid = options.resume if options.resume and not options.fork_session else ("chat-session" if calls[-1]["fork"] else "bg-session")
        yield sdk.SystemMessage(subtype="init", data={"session_id": sid, "fast_mode_state": "on" if fast else "off"})
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text=f"noted {len(calls)}")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id=sid, result="noted", total_cost_usd=0)
    monkeypatch.setattr(sdk, "query", fake_query)
    a.run_check("morning", "look at the day")                    # 1: standard session
    a.handle("on the chat thread", channel="app")                # 2: chat session (fork)
    a.settings.llm.fast_own_thread = False
    a.handle("on the one thread", channel="app")                 # 3: fast mode, but on the original session, caught up on 2
    assert (calls[2]["resume"], calls[2]["fork"], calls[2]["fast"]) == ("bg-session", False, True)
    assert "on the chat thread" in calls[2]["text"] and "noted 2" in calls[2]["text"]
    assert a.keep_fast_warm() is None                            # no chat session in use: nothing to keep alive
    a.settings.llm.fast_own_thread = True
    a.handle("back on the chat thread", channel="app")           # 4: the chat session again, caught up on 3
    assert (calls[3]["resume"], calls[3]["fork"]) == ("chat-session", False)
    assert "on the one thread" in calls[3]["text"] and "noted 3" in calls[3]["text"]
