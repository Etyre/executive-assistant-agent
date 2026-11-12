"""The agent is told when a shared document changes, with the diff, once the edit has settled (Eli, 2026-11-06)."""
import os
import pathlib
import tempfile
import time

from ea import config
from ea.agent.core import Agent
from ea.llm.base import LLMResponse
from ea.scheduler.jobs import Scheduler


def test_document_changes_reach_the_agent_with_a_diff(monkeypatch):
    config.load.cache_clear()
    s = config.load()
    s.sources.otter_logseq_graph = ""
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "Sleep.md").write_text("# Sleep\n- in bed by 00:30\n")
    s.document_roots = [config.DocumentRoot(name="site", path=str(d), note="the site")]
    prompts = []

    class P:
        name = "scripted"
        def complete(self, system, messages, tools, max_tokens, **kw):
            prompts.append([b["text"] for m in messages if m["role"] == "user" for b in m["content"] if b.get("type") == "text"][-1])
            return LLMResponse(content=[{"type": "tool_use", "id": "t", "name": "no_action_needed", "input": {"reason": "noted"}}], stop_reason="tool_use")
    a = Agent(s, provider=P())
    a.convo.set("docs_watch_started", None)
    sched = Scheduler(a, s)
    assert sched.watch_documents(settle_seconds=0) == [] and prompts == []          # first pass: snapshots only
    (d / "Sleep.md").write_text("# Sleep\n- in bed by 23:30\n- no screens after 23:00\n")
    old = time.time() - 10
    os.utime(d / "Sleep.md", (old, old))
    assert sched.watch_documents(settle_seconds=120) == []                            # too fresh: still being edited
    assert sched.watch_documents(settle_seconds=0) == ["site/Sleep.md"]
    assert "[document change: document changed: site/Sleep.md]" in prompts[0]
    assert "-- in bed by 00:30" in prompts[0] and "+- in bed by 23:30" in prompts[0] and "+- no screens" in prompts[0]
    assert sched.watch_documents(settle_seconds=0) == []                              # seen; not again
    (d / "New.md").write_text("# New\n")
    assert sched.watch_documents(settle_seconds=0) == [] and len(prompts) == 1          # a new file is just remembered
    (d / "New.md").write_text("# New\n- first line\n"); os.utime(d / "New.md", (old, old))
    assert sched.watch_documents(settle_seconds=0) == ["site/New.md"] and len(prompts) == 2
