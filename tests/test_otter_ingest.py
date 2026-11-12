"""The agent reads each new Otter recording in its own turn (Eli, 2026-11-06)."""
import datetime as dt
import pathlib
import tempfile

from ea import config
from ea.agent.core import Agent
from ea.llm.base import LLMResponse
from ea.scheduler.jobs import Scheduler


class Provider:
    name = "scripted"

    def __init__(self):
        self.prompts = []       # the user text that started each model call
        self.calls = 0

    def complete(self, system, messages, tools, max_tokens, **kw):
        self.calls += 1
        texts = [b["text"] for m in messages if m["role"] == "user" for b in m["content"] if b.get("type") == "text"]
        self.prompts.append(texts[-1])
        return LLMResponse(content=[{"type": "tool_use", "id": "t1", "name": "list_reminders", "input": {}}], stop_reason="tool_use") \
            if self.calls % 2 == 1 else LLMResponse(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn")


def _rec(otid, title, text, date="2026-11-06"):
    return {"otid": otid, "date": date, "start_time": "09:00", "title": title, "folder": "", "link": "", "text": text,
            "duration_s": 300, "source": "otter", "summary": None}


def test_new_recordings_are_read_once_each(monkeypatch):
    monkeypatch.setenv("OTTER_EMAIL", "e@x"); monkeypatch.setenv("OTTER_PASSWORD", "p")
    config.load.cache_clear()
    s = config.load()
    s.sources.otter_logseq_graph = ""
    prov = Provider()
    a = Agent(s, provider=prov)
    a.otter._upsert(_rec("old1", "Yesterday's walk", "old news", date="2026-11-05"))        # already there before ingestion starts
    pulled = []

    def fake_sync(max_recordings=40, all_history=False):
        for r in pulled:
            a.otter._upsert(r)
        return {"listed": len(pulled), "added": len(pulled), "already_had": 0, "still_processing": 0}
    monkeypatch.setattr(a.otter, "sync", fake_sync)
    t0 = dt.datetime(2026, 11, 6, 10, 0)
    sched = Scheduler(a, s, now_fn=lambda: t0)

    assert sched.ingest_otter() == []                       # first run: marks history seen, nothing new
    assert a.otter.unseen() == []
    pulled.append(_rec("new1", "Walking journal", "Remember to email Dave about the bills, and the carpet guy said Thursday."))
    pulled.append(_rec("new2", "Audio note", "Decided: skip the Tuesday reading group."))
    sched = Scheduler(a, s, now_fn=lambda: t0 + dt.timedelta(minutes=20))
    assert sched.ingest_otter() == ["new1", "new2"]
    assert len(prov.prompts) == 4                            # two turns, two model calls each
    assert "[otter recording: new Otter recording: Walking journal" in prov.prompts[0]
    assert "email Dave about the bills" in prov.prompts[0] and "skip the Tuesday reading group" in prov.prompts[2]
    # not again, and not within the sync interval
    sched = Scheduler(a, s, now_fn=lambda: t0 + dt.timedelta(minutes=25))
    assert sched.ingest_otter() == []
    sched = Scheduler(a, s, now_fn=lambda: t0 + dt.timedelta(minutes=60))
    assert sched.ingest_otter() == [] and len(prov.prompts) == 4
    # the ingestion turn's closing note is a log entry, not a chat message
    assert all(m["text"] != "ok" for m in a.convo.readable(10))
    assert any(b.get("text") == "ok" for m in a.convo.history(a.convo.current_thread(), 10) for b in m["content"] if isinstance(b, dict))


def test_ingestion_off_without_login(monkeypatch):
    for k in ("OTTER_EMAIL", "OTTER_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    config.load.cache_clear()
    s = config.load()
    s.sources.otter_logseq_graph = ""
    a = Agent(s, provider=Provider())
    assert Scheduler(a, s).ingest_otter() == []
