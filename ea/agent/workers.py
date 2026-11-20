"""Workers: the agent runs several tasks in parallel by delegating them (Eli, 2026-11-06).

A worker is one isolated turn with the same tools and prompt, its own conversation thread (`worker:<id>-<start time>`, kept
out of the chat history), its own browser, and no access to the user's conversation beyond what the task says.
When it finishes, its result goes to the main agent the way a message sent mid-turn does: folded into the
running turn, or starting a new proactive turn. The user's conversation itself stays single-threaded."""
from __future__ import annotations

import datetime as dt
import itertools
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

WORKER_ADDENDUM = """# YOU ARE A WORKER
You are a background worker that the main assistant delegated one task to. You have the same tools and memory.
Do the task completely, then end your turn with a clear, self-contained written result (what you found or did,
with specifics): that text is handed to the main assistant, who decides what to tell the user. Do not message
the user yourself (no send_message, no call_user) unless the task explicitly asks for it, do not schedule
wakeups, and do not end the conversation. If the task cannot be done, say exactly why in your result."""


class Worker:
    _ids = itertools.count(1)

    def __init__(self, name: str, task: str, requested_by: str):
        self.id = next(Worker._ids)
        self.name = name[:80]
        self.task = task
        self.requested_by = requested_by
        self.started = time.time()
        self.finished: float | None = None
        self.result: str | None = None
        self.error: str | None = None
        self.cancelled = False
        self.thread: threading.Thread | None = None

    @property
    def conv_thread(self) -> str:
        # The start time is part of the name: ids restart at 1 with every restart of the agent, and a worker over the
        # API engine reads its thread's history, so worker 1 used to begin with an older worker 1's conversation.
        return f"worker:{self.id}-{int(self.started)}"

    def describe(self) -> dict[str, Any]:
        d = {"id": self.id, "name": self.name, "task": self.task[:200], "started": dt.datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
             "running": self.finished is None, "minutes": round(((self.finished or time.time()) - self.started) / 60, 1)}
        if self.finished:
            d["finished"] = dt.datetime.fromtimestamp(self.finished).isoformat(timespec="seconds")
            d["outcome"] = "cancelled" if self.cancelled else ("error: " + self.error if self.error else "done")
        return d


class WorkerPool:
    def __init__(self, agent: Any, max_workers: int = 3, keep: int = 20):
        self.agent = agent
        self.max_workers = max_workers
        self.keep = keep
        self._workers: dict[int, Worker] = {}
        self._lock = threading.Lock()
        self.on_change: Callable[[list[dict[str, Any]]], None] | None = None   # the apps: who is running

    # ------------------------------------------------------------------ queries
    def running(self) -> list[Worker]:
        with self._lock:
            return [w for w in self._workers.values() if w.finished is None]

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            ws = sorted(self._workers.values(), key=lambda w: w.id)
        return [w.describe() for w in ws[-self.keep:]]

    def get(self, wid: int) -> Worker | None:
        with self._lock:
            return self._workers.get(wid)

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change([w.describe() for w in self.running()])
            except Exception:  # noqa: BLE001
                log.exception("workers on_change failed")

    # ------------------------------------------------------------------ lifecycle
    def start(self, name: str, task: str, requested_by: str = "agent") -> Worker:
        with self._lock:
            if sum(1 for w in self._workers.values() if w.finished is None) >= self.max_workers:
                raise RuntimeError(f"{self.max_workers} workers are already running; wait for one to finish or cancel one")
            w = Worker(name, task, requested_by)
            self._workers[w.id] = w
            done = sorted((x for x in self._workers.values() if x.finished), key=lambda x: x.id)
            for x in done[:-self.keep]:
                self._workers.pop(x.id, None)
        t = threading.Thread(target=self._run, args=(w,), name=w.conv_thread, daemon=True)
        t.ea_context = w.conv_thread           # type: ignore[attr-defined]  (the browser keeps a page per context)
        w.thread = t
        t.start()
        log.info("worker #%d '%s' started: %s", w.id, w.name, task[:120])
        self._changed()
        return w

    def cancel(self, wid: int) -> bool:
        w = self.get(wid)
        if w is None or w.finished is not None:
            return False
        w.cancelled = True                     # the turn loop checks this between steps
        return True

    def _run(self, w: Worker) -> None:
        try:
            w.result = self.agent.run_isolated(w.task, w.conv_thread, f"worker #{w.id} {w.name}", cancel=lambda: w.cancelled)
        except Exception as e:  # noqa: BLE001
            w.error = f"{type(e).__name__}: {e}"
            log.exception("worker #%d failed", w.id)
        finally:
            w.finished = time.time()
            browser = getattr(self.agent, "browser", None)
            if browser is not None:
                browser.close_context(w.conv_thread)   # its Chromium and browser thread, if it used the web
        log.info("worker #%d '%s' %s after %.1f min", w.id, w.name, "cancelled" if w.cancelled else "finished", (w.finished - w.started) / 60)
        self._changed()
        if w.cancelled:
            return
        head = f"[worker #{w.id} '{w.name}' finished ({w.describe()['minutes']} min)]"
        body = w.result if w.error is None else f"The worker failed: {w.error}"
        text = f"{head}\nTask: {w.task[:300]}\nResult:\n{body}"
        # Hand the result to the main agent: into a running turn, or as a turn of its own.
        if not self.agent.submit(text, channel="worker", client_id=""):
            try:
                self.agent.run_check(f"worker #{w.id} '{w.name}' finished", f"{text}\n\nDecide what to do with this result: act on it, "
                                     "remember what matters, and message the user only if they need to know now.", kind="worker result")
            except Exception:  # noqa: BLE001
                log.exception("handling worker #%d result failed", w.id)
