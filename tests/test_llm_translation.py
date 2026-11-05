from ea.llm.openai_compat_provider import from_openai_message, to_openai_messages


def test_anthropic_history_to_openai_messages():
    system = [{"type": "text", "text": "SYS"}, {"type": "text", "text": "MORE"}]
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."},
                                          {"type": "text", "text": "let me check"},
                                          {"type": "tool_use", "id": "t1", "name": "toggl_current", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "null", "is_error": False}]},
        {"role": "assistant", "content": [{"type": "text", "text": "nothing running"}]},
        {"role": "system", "content": "# NOW 12:00"},
    ]
    out = to_openai_messages(system, msgs)
    assert out[5] == {"role": "system", "content": "# NOW 12:00"}
    assert out[0] == {"role": "system", "content": "SYS\n\nMORE"}
    assert out[1] == {"role": "user", "content": "hi"}
    assert out[2]["role"] == "assistant" and out[2]["tool_calls"][0]["function"]["name"] == "toggl_current"
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "null"}
    assert out[4] == {"role": "assistant", "content": "nothing running"}


def test_openai_reply_to_blocks():
    r = from_openai_message({"content": "ok", "tool_calls": [{"id": "c1", "type": "function",
                                                              "function": {"name": "memory_search", "arguments": '{"query":"x"}'}}]}, "tool_calls")
    assert r.stop_reason == "tool_use"
    assert r.text == "ok" and r.tool_uses[0]["input"] == {"query": "x"}
    r2 = from_openai_message({"content": "bye"}, "stop")
    assert r2.stop_reason == "end_turn" and r2.tool_uses == []
    r3 = from_openai_message({"content": "trunc"}, "length")
    assert r3.stop_reason == "max_tokens"


def test_anthropic_history_breakpoint_is_on_last_conversation_message():
    from ea.llm.anthropic_provider import AnthropicProvider
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "yo"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "x"}]},
            {"role": "system", "content": "volatile"}]
    out = AnthropicProvider._with_history_breakpoint(msgs)
    assert out[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in msgs[2]["content"][0]          # original untouched
    assert out[3] == msgs[3] and "cache_control" not in out[1]["content"][0]
