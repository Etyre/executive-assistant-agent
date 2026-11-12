"""Delegation: run tasks in parallel with background workers (agent/workers.py)."""
from __future__ import annotations

from typing import Any

from ..agent.workers import WorkerPool
from .registry import ToolRegistry


def register(reg: ToolRegistry, pool: WorkerPool) -> None:
    @reg.tool("delegate", "Start a background worker on a task and continue immediately. The worker has your tools and memory "
              "but not this conversation, so put everything it needs in `task` (what to do, where to look, what a good "
              "result looks like). Its written result comes back to you when it finishes, as a message. Use it for "
              "anything long or independent: reading a lot of Roam or transcripts, web research, a sweep over the "
              f"tracker, several things at once (up to {pool.max_workers} workers).",
              {"properties": {"name": {"type": "string", "description": "Short label, e.g. 'Roam read: journals Oct 2026'."},
                              "task": {"type": "string"}},
               "required": ["name", "task"]}, writes=True)
    def delegate(name: str, task: str) -> Any:
        try:
            w = pool.start(name, task)
        except RuntimeError as e:
            return {"error": str(e), "running": [x["name"] for x in pool.status() if x["running"]]}
        return {"ok": True, "worker_id": w.id, "name": w.name, "note": "running in the background; its result will reach you as a message"}

    @reg.tool("workers_status", "The background workers: running ones (name, task, minutes) and the last finished ones.")
    def workers_status() -> Any:
        return pool.status()

    @reg.tool("cancel_worker", "Stop a running worker (it stops at its next step; no result is delivered).",
              {"properties": {"worker_id": {"type": "integer"}}, "required": ["worker_id"]}, writes=True)
    def cancel_worker(worker_id: int) -> Any:
        return {"ok": pool.cancel(worker_id)}
