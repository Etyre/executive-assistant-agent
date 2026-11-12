"""Workers: the agent delegates tasks that run in parallel; results come back into the main thread."""
import threading
import time

from ea import config
from ea.agent.core import Agent
from ea.llm.base import LLMResponse


def _agent(provider):
    config.load.cache_clear()
    s = config.load()
    s.sources.otter_logseq_graph = ""
    s.llm.max_workers = 2
    return Agent(s, provider=provider), s


class Scripted:
    """Per-thread scripts: workers and the main turn run concurrently, each on its own thread."""
    name = "scripted"

    def __init__(self):
        self.calls = []
        self.gate = threading.Event()        # workers wait here so we can observe them running in parallel

    def complete(self, system, messages, tools, max_tokens, **kw):
        texts = [b["text"] for m in messages if m["role"] == "user" for b in m["content"] if b.get("type") == "text"]
        first = texts[-1]                 # the newest user text (the store is shared across tests)
        self.calls.append((threading.current_thread().name, first[:60], any("YOU ARE A WORKER" in b.get("text", "") for b in system)))
        if first.startswith("[worker #"):
            self.gate.wait(5)
            which = "A" if "task A" in first else "B"
            return LLMResponse(content=[{"type": "text", "text": f"result of {which}: 42"}], stop_reason="end_turn")
        if first.startswith("[worker result:") or first.startswith("[worker results]"):
            return LLMResponse(content=[{"type": "text", "text": "noted the result"}], stop_reason="end_turn")
        return LLMResponse(content=[{"type": "text", "text": "main reply"}], stop_reason="end_turn")


def test_delegate_runs_workers_in_parallel_and_returns_results():
    p = Scripted()
    a, s = _agent(p)
    out1, err = a.tools.dispatch("delegate", {"name": "A", "task": "do task A"})
    out2, err2 = a.tools.dispatch("delegate", {"name": "B", "task": "do task B"})
    assert not err and not err2 and '"ok": true' in out1
    out3, _ = a.tools.dispatch("delegate", {"name": "C", "task": "do task C"})
    assert "already running" in out3                                   # cap of 2
    time.sleep(0.3)
    assert sorted(w.name for w in a.workers.running()) == ["A", "B"]
    assert [c[0] for c in p.calls if c[0].startswith("worker:")]       # both are inside their model call, in parallel
    assert all(c[2] for c in p.calls if c[0].startswith("worker:"))    # with the worker addendum in the system prompt
    p.gate.set()
    for w in list(a.workers.running()):
        w.thread.join(5)
    time.sleep(0.5)
    st = {w["name"]: w for w in a.workers.status()}
    assert st["A"]["outcome"] == "done" and st["B"]["outcome"] == "done"
    # each result started a proactive turn on the main thread; worker chatter stays out of the chat history
    results = [c for c in p.calls if c[1].startswith("[worker result")]
    assert len(results) == 2
    readable = [m["text"] for m in a.convo.readable(20)]
    assert not any(t.startswith("result of") for t in readable) and "noted the result" not in readable   # neither is chat
    main = [b.get("text") for m in a.convo.history(a.convo.current_thread(), 20) for b in m["content"] if isinstance(b, dict)]
    assert "noted the result" in main                                     # but the main thread did handle the results
    assert all(not m["text"].startswith("[worker #") for m in a.convo.readable(20))


def test_worker_result_folds_into_a_running_main_turn():
    p = Scripted()
    a, s = _agent(p)
    seen = []

    class Main:
        name = "scripted"
        calls = 0
        def complete(self, system, messages, tools, max_tokens, **kw):
            Main.calls += 1
            if Main.calls == 1:
                a.tools.dispatch("delegate", {"name": "A", "task": "do task A"})
                p.gate.set()
                for w in a.workers.running():
                    w.thread.join(5)                                   # the worker finishes while this turn is mid-step
                time.sleep(0.2)
                return LLMResponse(content=[{"type": "tool_use", "id": "t", "name": "list_reminders", "input": {}}], stop_reason="tool_use")
            seen.append([b.get("text", "") for b in messages[-2]["content"]])
            return LLMResponse(content=[{"type": "text", "text": "used the result"}], stop_reason="end_turn")
    a.provider = Main()
    a.workers.agent = a
    # the worker itself still uses the scripted worker provider: give the agent's worker path that provider
    orig = a.run_isolated
    def iso(task, thread, label, cancel=None):
        a.provider, main = p, a.provider
        try:
            return orig(task, thread, label, cancel)
        finally:
            a.provider = main
    a.run_isolated = iso
    assert a.handle("go", channel="app") == "used the result"
    assert any("[worker #" in t and "result of A: 42" in t for t in seen[0])     # folded in as a text block after the tool result


def test_cancel_worker():
    p = Scripted()
    a, s = _agent(p)
    out, _ = a.tools.dispatch("delegate", {"name": "A", "task": "do task A"})
    import json
    wid = json.loads(out)["worker_id"]
    assert json.loads(a.tools.dispatch("cancel_worker", {"worker_id": wid})[0])["ok"] is True
    p.gate.set()
    a.workers.get(wid).thread.join(5)
    assert a.workers.status()[-1]["outcome"] == "cancelled"


def test_finished_worker_closes_its_browser_context():
    p = Scripted(); p.gate.set()
    a, s = _agent(p)
    closed = []
    a.browser.close_context = closed.append
    w = a.workers.start("a", "task A: anything")
    w.thread.join(10)
    assert closed == [w.conv_thread]
