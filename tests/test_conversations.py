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


def test_readable_history_is_what_a_person_would_see(tmp_path):
    from ea.agent.conversations import ConversationStore
    c = ConversationStore(tmp_path / "s.db", tmp_path / "t")
    th = c.current_thread()
    c.append(th, "user", [{"type": "text", "text": "[scheduled check: evening]\nlook"}], "scheduler")
    c.append(th, "assistant", [{"type": "tool_use", "id": "u1", "name": "tracker_feed", "input": {}}], "scheduler")
    c.append(th, "user", [{"type": "tool_result", "tool_use_id": "u1", "content": "{}"}], "scheduler")
    c.append(th, "assistant", [{"type": "text", "text": "[sent via send_message] Task 76 is due tomorrow."}], "proactive")
    c.append(th, "user", [{"type": "text", "text": "thanks"}], "web")
    c.append(th, "assistant", [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Any time."}], "web")
    r = c.readable()
    assert [(m["who"], m["text"], m["channel"]) for m in r] == [
        ("it", "Task 76 is due tomorrow.", "proactive"), ("me", "thanks", "web"), ("it", "Any time.", "web")]
    assert c.readable(limit=1)[0]["text"] == "Any time."


def test_reads_and_writes_from_many_threads_never_lose_a_value(tmp_path):
    """One connection, many threads: an unlocked read used to raise or, worse, return the default for a key that exists."""
    import threading
    from ea.agent.conversations import ConversationStore
    store = ConversationStore(tmp_path / "state.db", tmp_path / "transcripts")
    store.set("present", {"v": 1})
    bad, stop = [], threading.Event()

    def read():
        while not stop.is_set():
            try:
                if store.get("present") != {"v": 1}:
                    bad.append("missing")
                store.last_id("main"); store.pending_reminders(); store.history("main")
            except Exception as e:  # noqa: BLE001
                bad.append(repr(e))

    def write(i):
        for n in range(150):
            store.set(f"k{i}", n); store.append("main", "user", f"m{i}-{n}", "web")
    readers = [threading.Thread(target=read) for _ in range(3)]
    writers = [threading.Thread(target=write, args=(i,)) for i in range(2)]
    [t.start() for t in readers + writers]; [t.join() for t in writers]; stop.set(); [t.join() for t in readers]
    assert bad == []


def _valid_for_the_api(msgs):
    """The API's rule: the user turn right after an assistant turn answers each of its tool calls, and nothing else is a result."""
    merged = []
    for m in msgs:
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        if merged and merged[-1][0] == m["role"]:
            merged[-1][1].extend(blocks)
        else:
            merged.append([m["role"], list(blocks)])
    for n, (role, blocks) in enumerate(merged):
        calls = [b["id"] for b in blocks if b.get("type") == "tool_use"] if role == "assistant" else []
        nxt = merged[n + 1][1] if n + 1 < len(merged) else []
        answers = [b["tool_use_id"] for b in nxt if b.get("type") == "tool_result"]
        if role == "assistant" and (sorted(calls) != sorted(answers) or any(b.get("type") != "tool_result" for b in nxt[:len(calls)])):
            return False
        if role == "user" and (n == 0 or merged[n - 1][0] != "assistant") and any(b.get("type") == "tool_result" for b in blocks):
            return False
    return True


def test_history_handed_to_the_model_is_repaired_but_the_stored_rows_are_not():
    from ea.agent.conversations import INTERRUPTED, repair
    use = lambda i: {"type": "tool_use", "id": i, "name": "t", "input": {}}
    res = lambda i: {"type": "tool_result", "tool_use_id": i, "content": "ok", "is_error": False}
    text = lambda t: {"type": "text", "text": t}
    typed_between = [{"role": "user", "content": [text("go")]}, {"role": "assistant", "content": [use("a")]},
                     {"role": "user", "content": [text("typed while it worked")]}, {"role": "user", "content": [res("a")]},
                     {"role": "assistant", "content": [text("done")]}]
    never_answered = [{"role": "user", "content": "go"}, {"role": "assistant", "content": [text("let me look"), use("b"), use("c")]},
                      {"role": "user", "content": [res("c")]}, {"role": "user", "content": "next message after a restart"}]
    orphan = [{"role": "user", "content": [res("zz"), text("hello")]}, {"role": "assistant", "content": [text("hi")]},
              {"role": "user", "content": [res("yy")]}, {"role": "user", "content": "and then"}]
    for case in (typed_between, never_answered, orphan):
        import copy
        before = copy.deepcopy(case)
        assert not _valid_for_the_api(case)
        fixed = repair(case)
        assert _valid_for_the_api(fixed), fixed
        assert case == before                                   # the input is not edited in place
    fixed = repair(never_answered)
    assert [b["content"] for b in fixed[2]["content"]] == [INTERRUPTED, "ok"] and fixed[-1]["content"] == "next message after a restart"
    assert [b.get("text") for m in repair(typed_between) for b in m["content"] if b.get("type") == "text"] == ["go", "typed while it worked", "done"]
    good = [{"role": "user", "content": "q"}, {"role": "assistant", "content": [use("a")]}, {"role": "user", "content": [res("a")]},
            {"role": "assistant", "content": [text("answer")]}]
    assert repair(good) == good
