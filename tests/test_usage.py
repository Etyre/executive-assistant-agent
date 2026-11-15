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


def test_per_day_and_component_breakdowns_and_the_agents_tool(tmp_path):
    c = _store(tmp_path)
    opus = dict(engine="agent_sdk", billing="subscription", model="claude-opus-5-5")
    usage.record(c, **opus, mode="chat", usage={"speed": "fast", "input_tokens": 2, "output_tokens": 100,
                                               "cache_creation": {"ephemeral_1h_input_tokens": 200_000}})      # a cold start: $3.20 + output
    usage.record(c, **opus, mode="chat", usage={"speed": "fast", "input_tokens": 2, "output_tokens": 100, "cache_read_input_tokens": 200_000})
    usage.record(c, **opus, mode="keepalive", usage={"speed": "fast", "input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 200_000})
    usage.record(c, **opus, mode="proactive", usage={"input_tokens": 2, "output_tokens": 500, "cache_read_input_tokens": 300_000})
    today = usage.daily(c, 3)[-1]
    assert today["fast_calls"] == 3 and today["cold_starts"] == 1 and today["cold_start_usd"] == pytest.approx(3.204, abs=0.001)
    assert today["keepalive"] == pytest.approx(0.0802, abs=0.001) and today["chat"] == pytest.approx(3.204 + 0.084, abs=0.001)
    assert today["charged"] == pytest.approx(today["chat"] + today["keepalive"]) == usage.charged_today(c)
    assert today["plan_list_usd"] == pytest.approx(0.07, abs=0.001)                  # the standard call: counted, not charged
    assert usage.daily(c, 3)[0]["calls"] == 0                                        # two days ago: nothing
    text = usage.breakdown_report(c, 7)
    assert "cache writes (1 hour)" in text and "cache reads" in text and "keepalive" in text and "proactive" in text
    parts = usage.component_costs("claude-opus-5-5", "fast", {"cache_write_1h": 1_000_000, "output": 1_000_000})
    assert parts["cache_write_1h"] == 16 and parts["output"] == 40 and sum(parts.values()) == usage.cost("claude-opus-5-5", "fast", usage.tokens({"output_tokens": 1_000_000, "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}))
    # the agent reads all of it through its own tool
    from ea.tools import assistant_tools
    from ea.tools.registry import ToolRegistry
    reg = ToolRegistry()
    assistant_tools.register(reg, c, config.load(), lambda t: None, lambda r: None)
    out, err = reg.dispatch("model_usage", {"days": 3})
    assert not err and "Model usage by speed" in out and "Charged beyond the plan, per day" in out and "Cost by component" in out and "Cost by kind of turn" in out


def test_scheduler_alerts_once_a_day_above_the_line_and_reviews_every_week():
    from ea.scheduler.jobs import Scheduler
    config.load.cache_clear()
    s = config.load()
    s.llm.provider = "anthropic"
    s.sources.otter_logseq_graph = ""
    ran = []

    class P:
        name, model = "anthropic", "claude-opus-5-5"

        def complete(self, *, system, messages, tools, max_tokens, on_text=None, fast=False):
            ran.append(next(b["text"] for b in messages[-2]["content"] if b.get("type") == "text") if isinstance(messages[-2]["content"], list) else messages[-2]["content"])
            return LLMResponse(content=[{"type": "text", "text": "noted"}], stop_reason="end_turn")
    a = Agent(s, provider=P())
    for k in ("cost_alert_day", "cost_review_next"):
        a.convo.set(k, None)
    a.convo._conn.execute("DELETE FROM llm_usage"); a.convo._conn.commit()
    t0 = dt.datetime.now(dt.timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo(s.timezone)).replace(hour=12, minute=0)
    sched = Scheduler(a, s, now_fn=lambda: t0)
    assert sched.watch_costs(t0) == [] and a.convo.get("cost_review_next") == pytest.approx((t0 + dt.timedelta(days=7)).timestamp())
    usage.record(a.convo, engine="agent_sdk", billing="subscription", model="claude-opus-5-5", mode="chat",
                 usage={"speed": "fast", "input_tokens": 1, "output_tokens": 1, "cache_creation": {"ephemeral_1h_input_tokens": 10_000_000}})   # $160
    a.convo._conn.execute("UPDATE llm_usage SET ts = ?", (t0.timestamp() - 60,)); a.convo._conn.commit()
    assert sched.watch_costs(t0) == ["alert"] and "[cost alert: model spend]" in ran[-1] and "$160.00" in ran[-1] and "$150 a day" in ran[-1]
    assert sched.watch_costs(t0) == []                                               # once a day
    assert sched.watch_costs(t0 + dt.timedelta(days=6)) == []                        # a new day, nothing spent, review not due
    week = t0 + dt.timedelta(days=7, minutes=1)
    assert sched.watch_costs(week.replace(hour=3)) == []                             # due, but in the night: waits for the active window
    assert sched.watch_costs(week) == ["review"] and "[cost review: model costs, every 7 days]" in ran[-1] and "model_usage" in ran[-1]
    assert sched.watch_costs(week) == []
