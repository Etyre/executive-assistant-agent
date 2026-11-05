"""The agent loop: one user turn in, tool calls executed until the model stops, one reply out.

Provider-agnostic (see llm/), channel-agnostic (callers pass a channel label and a mode), and
single-threaded per conversation (a lock), because all channels feed the same thread."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .. import config
from ..connectors.calendar import Calendar
from ..connectors.exercise import Exercise
from ..connectors.http import ServiceRouter
from ..connectors.toggl import Toggl
from ..llm.base import LLMError, Provider
from ..llm.factory import make_provider
from ..memory.store import MemoryStore
from ..tools import assistant_tools, data_tools, memory_tools, tracker_tools
from ..tools.registry import ToolRegistry
from . import prompt
from .conversations import ConversationStore

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, settings: config.Settings | None = None, provider: Provider | None = None,
                 router: ServiceRouter | None = None):
        self.settings = settings or config.load()
        self.provider = provider or make_provider(self.settings)
        self.memory = MemoryStore(self.settings.memory_dir)
        self.convo = ConversationStore(self.settings.state_db, self.settings.transcripts_dir)
        self.router = router or ServiceRouter(self.settings)
        self.toggl = Toggl(self.settings)
        self.exercise = Exercise(self.settings)
        self.tools = ToolRegistry()
        self._channel = "system"
        self._lock = threading.RLock()
        self.outbound: Callable[[str], None] | None = None     # set by the serving process (Telegram)
        self._ended: str | None = None
        self.tracker = tracker_tools.register(self.tools, self.router, self.settings)
        self.calendar = Calendar(self.settings, tracker=self.tracker)
        data_tools.register(self.tools, self.router, self.toggl, self.exercise, self.calendar)
        memory_tools.register(self.tools, self.memory, lambda: self._channel)
        assistant_tools.register(self.tools, self.convo, self.settings, self._send_outbound, self._end_conversation)

    # ------------------------------------------------------------------ plumbing
    def _send_outbound(self, text: str) -> None:
        if self.outbound:
            self.outbound(text)
        else:
            log.info("outbound (no channel attached): %s", text)
        self.convo.append(self.convo.current_thread(), "assistant", [{"type": "text", "text": f"[sent via send_message] {text}"}],
                          channel="proactive")

    def _end_conversation(self, reason: str) -> None:
        self._ended = reason
        log.info("agent ended the conversation: %s", reason)

    def run_check(self, name: str, check_prompt: str) -> str | None:
        """A scheduled, proactive turn. Returns the final text (not sent unless the model used send_message)."""
        text = f"[scheduled check: {name}]\n{check_prompt}"
        return self.handle(text, channel="scheduler", mode="proactive", store_user=True)

    # ------------------------------------------------------------------ main entry
    def handle(self, user_text: str, channel: str = "chat", mode: str = "chat", store_user: bool = True) -> str:
        """Process one message from the user (or the scheduler) and return the reply text."""
        with self._lock:
            self._channel = channel
            thread = self.convo.current_thread()
            if self.convo.get("ended_note"):
                user_text = ("[system note: you ended the previous conversation. The user may want to debrief or start "
                             "fresh; follow their lead.]\n" + user_text)
                self.convo.set("ended_note", None)
            if store_user:
                self.convo.append(thread, "user", [{"type": "text", "text": user_text}], channel)
            system = prompt.build_system(self.settings)
            messages = self.convo.history(thread, self.settings.llm.history_messages)
            if not messages:
                messages = [{"role": "user", "content": [{"type": "text", "text": user_text}]}]
            final_text = ""
            for _round in range(self.settings.llm.max_tool_rounds):
                volatile = prompt.volatile_message(self.settings, self.memory, self.convo, channel, mode)
                try:
                    resp = self.provider.complete(system=system, messages=messages + [volatile], tools=self.tools.specs(),
                                                  max_tokens=self.settings.llm.max_tokens)
                except LLMError as e:
                    log.error("LLM error: %s", e)
                    return f"(model error: {e})"
                self.convo.append(thread, "assistant", resp.content, channel)
                messages.append({"role": "assistant", "content": resp.content})
                if resp.text:
                    final_text = resp.text
                if resp.stop_reason != "tool_use" or not resp.tool_uses:
                    break
                results = []
                for tu in resp.tool_uses:
                    out, err = self.tools.dispatch(tu["name"], tu.get("input") or {})
                    log.info("tool %s(%s) -> %s%s", tu["name"], _short(tu.get("input")), "ERROR " if err else "", _short(out, 160))
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out, "is_error": err})
                self.convo.append(thread, "user", results, channel)
                messages.append({"role": "user", "content": results})
            else:
                final_text = final_text or "(stopped after too many tool calls in one turn)"
            if self._ended:
                self.convo.new_thread()
                self.convo.set("ended_note", self._ended)
                self._ended = None
            return final_text or "(no reply)"


def _short(x: Any, n: int = 100) -> str:
    s = str(x).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"
