"""Tokens and estimated cost per model call, split by the speed the API served it at (fast vs standard)."""
import datetime as dt

import claude_agent_sdk as sdk
import pytest

from ea import config, usage
from ea.agent.conversations import ConversationStore
from ea.agent.core import Agent
from ea.llm.base import LLMResponse


def _store(tmp_path):
    return ConversationStore(tmp_path / "state.db", tmp_path / "transcripts")


def test_cost_at_list_prices():
    t = usage.tokens({"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
                      "cache_creation_input_tokens": 3_000_000,
                      "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 2_000_000}})
    assert t == {"input": 1_000_000, "cache_read": 1_000_000, "cache_write_5m": 1_000_000, "cache_write_1h": 2_000_000, "output": 1_000_000}
    std = usage.cost("claude-opus-5-5", "standard", t)
    assert std == pytest.approx(4 + 20 + 0.20 + 4 * 1.25 + 2 * 4 * 2.0)
    assert usage.cost("claude-opus-5-5", "fast", t) == pytest.approx(2 * std)
    assert usage.cost("some-local-model", "standard", t) is None                   # counted, not priced
    # matches what the Claude Code CLI itself estimated for a live call on 2026-11-14 (1,325 tokens written to the 1h cache)
    live = usage.tokens({"input_tokens": 2, "output_tokens": 4, "cache_creation": {"ephemeral_1h_input_tokens": 1325}})
    assert usage.cost("claude-opus-5-5", "standard", live) == pytest.approx(0.0107, abs=0.0001)


def test_totals_and_report_split_by_speed(tmp_path):
    c = _store(tmp_path)
    assert "nothing recorded yet" in usage.report(c)
    u = {"input_tokens": 100, "output_tokens": 1000, "cache_read_input_tokens": 10_000}
    usage.record(c, engine="agent_sdk", billing="subscription", model="claude-opus-5-5", usage={**u, "speed": "fast"}, mode="chat")
    usage.record(c, engine="agent_sdk", billing="subscription", model="claude-opus-5-5", usage=u, mode="proactive")
    usage.record(c, engine="agent_sdk", billing="subscription", model="claude-opus-5-5", usage={**u, "speed": "standard"}, mode="proactive")
    usage.record(c, engine="x", billing="api", model="m", usage={})                 # nothing to count: no row
    rows = {r["speed"]: r for r in usage.summary(c, 30)}
    assert rows["fast"]["calls"] == 1 and rows["standard"]["calls"] == 2
    assert rows["fast"]["tokens"] == 11_100 and rows["standard"]["tokens"] == 22_200
    assert rows["fast"]["cost_usd"] == pytest.approx(rows["standard"]["cost_usd"])    # half the calls at twice the price
    text = usage.report(c)
    assert "credits (extra usage)" in text and "plan (not charged)" in text and "today:" in text
    # a call from last week is in the 30-day totals but not in today's
    c._conn.execute("UPDATE llm_usage SET ts = ts - 3 * 86400 WHERE speed = 'fast'"); c._conn.commit()
    assert [r["speed"] for r in usage.summary(c, 0, dt.datetime.now())] == ["standard"]


def test_api_engine_records_each_call_with_its_speed():
    class P:
        name, supports_fast, model = "anthropic", True, "claude-opus-5-5"

        def complete(self, *, system, messages, tools, max_tokens, on_text=None, fast=False):
            return LLMResponse(content=[{"type": "text", "text": "noted"}], stop_reason="end_turn", model="claude-opus-5-5",
                               usage={"input_tokens": 7, "output_tokens": 3, "speed": "fast" if fast else "standard"})
    config.load.cache_clear()
    s = config.load()
    s.llm.provider = "anthropic"
    s.sources.otter_logseq_graph = ""
    a = Agent(s, provider=P())
    before = a.convo._conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    a.handle("hello", channel="app")
    a.run_check("morning", "look at the day")
    rows = [dict(r) for r in a.convo._conn.execute("SELECT speed, mode, billing, input, output FROM llm_usage ORDER BY id")][before:]
    assert rows == [{"speed": "fast", "mode": "chat", "billing": "api", "input": 7, "output": 3},
                    {"speed": "standard", "mode": "proactive", "billing": "api", "input": 7, "output": 3}]


def test_sdk_engine_records_from_the_stream(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    s.llm.provider = "agent_sdk"
    s.sources.otter_logseq_graph = ""
    a = Agent(s)
    a.convo.set("sdk_api_fallback_until", None)

    def ev(e):
        return sdk.StreamEvent(uuid="u", session_id="s1", event=e)

    async def fake_query(*, prompt, options=None, transport=None):
        await prompt.__anext__()
        yield sdk.SystemMessage(subtype="init", data={"session_id": "s1", "fast_mode_state": "on"})
        for n in (1, 2):                                    # two model calls in the turn
            yield ev({"type": "message_start", "message": {"id": f"m{n}", "model": "claude-opus-5-5", "usage": {
                "input_tokens": 5, "output_tokens": 1, "cache_read_input_tokens": 2000, "speed": "fast",
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 300}}}})
            yield ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 40 * n, "speed": None}})
            yield ev({"type": "message_stop"})
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text="noted")], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s1", result="noted", total_cost_usd=0)
    monkeypatch.setattr(sdk, "query", fake_query)
    before = a.convo._conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    a.handle("hello", channel="app")
    rows = [dict(r) for r in a.convo._conn.execute("SELECT * FROM llm_usage ORDER BY id")][before:]
    assert [(r["speed"], r["engine"], r["billing"], r["mode"], r["output"], r["cache_read"], r["cache_write_1h"]) for r in rows] == \
        [("fast", "agent_sdk", "subscription", "chat", 40, 2000, 300), ("fast", "agent_sdk", "subscription", "chat", 80, 2000, 300)]
    assert rows[0]["cost_usd"] == pytest.approx(2 * (5 * 4 + 300 * 8 + 2000 * 0.20 + 40 * 20) / 1e6)
