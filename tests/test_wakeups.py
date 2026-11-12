"""Self-scheduled wakeups (the agent's own future turns), the random heartbeat, and the store migration that
adds both to a database from before they existed."""
import datetime as dt
import random
import sqlite3

import pytest

from ea.agent.conversations import ConversationStore
from ea.scheduler.jobs import HEARTBEAT_KEY, Scheduler
from tests.test_tools_and_agent import agent, text, tool  # noqa: F401  (fixture + helpers)

UTC = dt.timezone.utc


def test_store_migrates_old_reminders_table(tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript("""CREATE TABLE reminders (id INTEGER PRIMARY KEY, due_ts REAL NOT NULL, text TEXT NOT NULL,
                          channel TEXT DEFAULT 'telegram', created_ts REAL NOT NULL, sent_ts REAL);
                          INSERT INTO reminders(due_ts, text, created_ts) VALUES (1, 'old one', 1);""")
    conn.commit(); conn.close()
    s = ConversationStore(db, tmp_path / "t")
    old = s.pending_reminders()
    assert old[0]["text"] == "old one" and old[0]["kind"] == "message" and old[0]["every_minutes"] is None
    wid = s.add_reminder(dt.datetime.now(UTC), "look again", kind="wakeup", every_minutes=30)
    assert [r["id"] for r in s.pending_reminders("wakeup")] == [wid]


def test_repeating_reminder_rearms_and_skips_missed_periods(tmp_path):
    s = ConversationStore(tmp_path / "state.db", tmp_path / "t")
    t0 = dt.datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
    rid = s.add_reminder(t0, "every hour", kind="wakeup", every_minutes=60)
    s.mark_sent(rid, now=t0.timestamp() + 5)
    (r,) = s.pending_reminders()
    assert r["due_ts"] == (t0 + dt.timedelta(hours=1)).timestamp()        # one-off would be gone; repeating moved on
    s.mark_sent(rid, now=(t0 + dt.timedelta(hours=3, minutes=30)).timestamp())   # machine was asleep: 10:00..13:00 missed
    (r,) = s.pending_reminders()
    assert r["due_ts"] == (t0 + dt.timedelta(hours=4)).timestamp()
    one = s.add_reminder(t0, "once", kind="message")
    s.mark_sent(one, now=t0.timestamp())
    assert [x["id"] for x in s.pending_reminders()] == [rid]


def test_wakeup_tool_runs_a_turn_later(agent):
    sent = []
    agent.outbound = sent.append
    sched = Scheduler(agent, agent.settings, rng=random.Random(1))
    agent.settings.heartbeat.enabled = False
    past = (dt.datetime.now() - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    agent.provider.script = [tool("schedule_wakeup", {"when": past, "prompt": "See whether the memo block happened; message if not."}), text("will do")]
    agent.handle("check back on that later", channel="cli")
    (w,) = agent.convo.pending_reminders("wakeup")
    assert w["text"].startswith("See whether") and w["every_minutes"] is None
    agent.provider.script = [tool("send_message", {"text": "The memo block did not happen."}), text("sent")]
    sched.tick()
    assert sent == ["The memo block did not happen."]
    assert agent.convo.pending_reminders() == []
    wake_turn = agent.provider.calls[2]["messages"]              # calls 0-1 were the chat turn; 2 opens the wakeup turn
    first_user = [m for m in wake_turn if m["role"] == "user" and m["content"][0].get("type") == "text"][-1]["content"][0]["text"]
    assert first_user.startswith("[wakeup: wakeup #") and "See whether the memo block happened" in first_user
    # the list/cancel tools cover wakeups too
    agent.provider.script = [tool("schedule_wakeup", {"when": "2030-01-01T09:00", "prompt": "p", "every_minutes": 45}),
                             tool("list_reminders", {}, "u2"), tool("cancel_reminder", {"reminder_id": 2}, "u3"), text("ok")]
    agent.handle("set and cancel", channel="cli")
    listed = agent.provider.calls[-2]["messages"][-2]["content"][0]["content"]
    assert '"kind": "wakeup"' in listed and '"every_minutes": 45' in listed
    assert agent.provider.calls[-1]["messages"][-2]["content"][0]["content"] == "cancelled"


def _at(h, m=0):
    return dt.datetime(2026, 11, 5, h, m, tzinfo=UTC)


def test_heartbeat_schedules_persists_and_fires(agent):
    agent.settings.heartbeat.enabled = True
    agent.settings.heartbeat.mean_minutes = 60
    agent.settings.heartbeat.min_minutes = 20
    agent.settings.heartbeat.max_minutes = 180
    agent.settings.heartbeat.active_start, agent.settings.heartbeat.active_end = "08:00", "23:00"
    clock = {"now": _at(10, 0)}
    sched = Scheduler(agent, agent.settings, rng=random.Random(7), now_fn=lambda: clock["now"])
    sent = []
    agent.outbound = sent.append
    # first tick only arms the clock, never fires
    sched.tick()
    nxt = sched.next_heartbeat()
    assert nxt is not None and _at(10, 20) <= nxt <= _at(13, 0) and agent.provider.calls == []
    # a restart (new scheduler) keeps the same next time
    sched2 = Scheduler(agent, agent.settings, rng=random.Random(8), now_fn=lambda: clock["now"])
    assert sched2.next_heartbeat() == nxt
    # before it is due: nothing
    clock["now"] = nxt - dt.timedelta(minutes=1)
    sched2.tick()
    assert agent.provider.calls == []
    # due: one proactive turn, and the next time is drawn before the turn runs
    clock["now"] = nxt
    agent.provider.script = [tool("no_action_needed", {"reason": "quiet"}), text("nothing")]
    sched2.tick()
    assert len(agent.provider.calls) == 1 and sent == []          # no_action_needed ends the turn
    first_user = agent.provider.calls[0]["messages"][0]["content"][0]["text"]
    assert first_user.startswith("[heartbeat: ") and "woke up on your own" in first_user
    after = sched2.next_heartbeat()
    assert nxt + dt.timedelta(minutes=20) <= after <= nxt + dt.timedelta(minutes=180)
    assert sched2.in_active_window(after)


def test_heartbeat_respects_active_window_and_disabled(agent):
    hb = agent.settings.heartbeat
    hb.enabled, hb.active_start, hb.active_end = True, "08:00", "23:00"
    clock = {"now": _at(2, 0)}
    sched = Scheduler(agent, agent.settings, rng=random.Random(3), now_fn=lambda: clock["now"])
    sched.tick()                                               # 02:00: arm for after 08:00
    nxt = sched.next_heartbeat()
    assert _at(8, 20) <= nxt <= _at(11, 0)
    # the machine slept through the whole day; at 23:30 the stale due time must not fire, just re-arm for tomorrow
    clock["now"] = _at(23, 30)
    sched.tick()
    assert agent.provider.calls == []
    assert sched.next_heartbeat() >= _at(8, 0) + dt.timedelta(days=1)
    # gaps are clamped
    hb.mean_minutes, hb.min_minutes, hb.max_minutes = 60, 20, 90
    gaps = [sched.draw_gap() for _ in range(200)]
    assert min(gaps) >= dt.timedelta(minutes=20) and max(gaps) <= dt.timedelta(minutes=90)
    # a wrap-around window (22:00-06:00) is handled
    hb.active_start, hb.active_end = "22:00", "06:00"
    assert sched.in_active_window(_at(23, 0)) and sched.in_active_window(_at(3, 0)) and not sched.in_active_window(_at(12, 0))
    # disabled: nothing is armed or run
    hb.enabled = False
    agent.convo.set(HEARTBEAT_KEY, None)
    clock["now"] = _at(12, 0)
    sched.tick()
    assert sched.next_heartbeat() is None and agent.provider.calls == []


def test_no_action_needed_ends_the_turn(monkeypatch):
    """A proactive turn stops at no_action_needed instead of looping to the round limit (2026-11-06)."""
    from ea import config
    from ea.agent.core import Agent
    from ea.llm.base import LLMResponse
    config.load.cache_clear()
    s = config.load()
    s.sources.otter_logseq_graph = ""
    calls = []

    class P:
        name = "scripted"
        def complete(self, **kw):
            calls.append(1)
            return LLMResponse(content=[{"type": "tool_use", "id": f"t{len(calls)}", "name": "no_action_needed", "input": {"reason": "all quiet"}}], stop_reason="tool_use")
    a = Agent(s, provider=P())
    out = a.run_check("evening", "Look around.")
    assert len(calls) == 1 and out == "all quiet"
