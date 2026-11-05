import datetime as dt
import json

from ea.agent.conversations import ConversationStore, window


def test_window_never_splits_tool_pairs_and_starts_with_user():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "a"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "r"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {"role": "user", "content": [{"type": "text", "text": "b"}]},
    ]
    w = window(msgs, 3)       # would cut inside the tool pair; must move to the next plain user message
    assert w == msgs[4:]
    assert window(msgs, 10) == msgs
    assert window(msgs[1:3], 10) == []


def test_store_roundtrip_and_transcripts(tmp_path):
    s = ConversationStore(tmp_path / "state.db", tmp_path / "t")
    s.append("main", "user", [{"type": "text", "text": "hi"}], channel="telegram")
    s.append("main", "assistant", [{"type": "text", "text": "hello"}], channel="telegram")
    h = s.history("main")
    assert [m["role"] for m in h] == ["user", "assistant"]
    day = dt.date.today().isoformat()
    lines = (tmp_path / "t" / f"{day}.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["channel"] == "telegram"
    assert s.recent_days(2) == [day]


def test_reminders_and_threads(tmp_path):
    s = ConversationStore(tmp_path / "state.db", tmp_path / "t")
    past = dt.datetime.now() - dt.timedelta(minutes=1)
    future = dt.datetime.now() + dt.timedelta(hours=1)
    a = s.add_reminder(past, "now-ish")
    b = s.add_reminder(future, "later")
    assert [r["id"] for r in s.due_reminders()] == [a]
    s.mark_sent(a)
    assert s.due_reminders() == []
    assert [r["text"] for r in s.pending_reminders()] == ["later"]
    assert s.cancel_reminder(b) and not s.cancel_reminder(b)
    assert s.current_thread() == "main"
    t = s.new_thread()
    assert t.startswith("main@") and s.current_thread() == t
