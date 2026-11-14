"""The agent loop: one user turn in, tool calls executed until the model stops, one reply out.

Provider-agnostic (see llm/), channel-agnostic (callers pass a channel label and a mode), and
single-threaded per conversation (a lock), because all channels feed the same thread."""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .. import config, contact, usage
from ..connectors.calendar import Calendar
from ..connectors.exercise import Exercise
from ..connectors.http import ServiceRouter
from ..connectors.otter import Otter
from ..connectors.roam import Roam
from ..connectors.browser import Browser
from ..connectors.toggl import Toggl
from ..llm.base import LLMError, Provider
from ..llm.factory import make_provider
from ..memory.store import MemoryStore
from ..tools import assistant_tools, data_tools, memory_tools, otter_tools, roam_tools, tracker_tools, web_tools, worker_tools, workspace_tools
from ..tools.registry import ToolRegistry
from . import prompt
from .conversations import MID_TURN_PREFIX, ConversationStore, _clip
from .workers import WORKER_ADDENDUM, WorkerPool
from ..images import ImageStore
from ..workspace import Documents

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, settings: config.Settings | None = None, provider: Provider | None = None,
                 router: ServiceRouter | None = None):
        self.settings = settings or config.load()
        self.provider = provider or make_provider(self.settings)
        self.memory = MemoryStore(self.settings.memory_dir)
        self.convo = ConversationStore(self.settings.state_db, self.settings.transcripts_dir)
        self.router = router or ServiceRouter(self.settings)
        self.toggl = Toggl(self.settings, self.router)
        self.exercise = Exercise(self.settings)
        self.otter = Otter(self.settings)
        self.roam = Roam(self.settings)
        self.documents = Documents(self.settings)
        self.browser = Browser()
        self.workspace = self.documents.whiteboard()
        self.tools = ToolRegistry()
        self._channel = "system"
        self._lock = threading.RLock()
        self.outbound: Callable[[str], None] | None = None     # set by the serving process (push/live feed)
        self.activity: Callable[[dict[str, Any]], None] | None = None   # chain-of-thought feed, every turn (see conversations.activity)
        self._ended: str | None = None
        self._inbox: list[dict[str, Any]] = []               # messages the user sent while a turn was running
        self._inbox_lock = threading.Lock()
        self._turn_running = False
        self.on_message: Callable[[dict[str, Any]], None] | None = None   # every chat message, both ways (live feed)
        self.tracker = tracker_tools.register(self.tools, self.router, self.settings)
        self.calendar = Calendar(self.settings, tracker=self.tracker)
        data_tools.register(self.tools, self.router, self.toggl, self.exercise, self.calendar)
        otter_tools.register(self.tools, self.otter)
        roam_tools.register(self.tools, self.roam)
        if self.settings.sources.web_browsing:
            web_tools.register(self.tools, self.browser)
        self.ui: Callable[[dict[str, Any]], None] | None = None      # set by the server: {"type":"ui","action":...} to the apps
        self.images = ImageStore(self.settings.data_dir / "images")
        self.workers = WorkerPool(self, max_workers=self.settings.llm.max_workers)
        worker_tools.register(self.tools, self.workers)
        workspace_tools.register(self.tools, self.workspace, show=lambda name="workspace": self.ui and self.ui({"action": "show_workspace", "doc": name}),
                                 docs=self.documents)
        memory_tools.register(self.tools, self.memory, lambda: self._channel)
        assistant_tools.register(self.tools, self.convo, self.settings, self._send_outbound, self._end_conversation)
        contact.register(self.tools)                          # the model can reach Eli (shared LLM inbox)
        self.sdk = None
        self.api_provider: Provider | None = None            # the fallback when the subscription is nearly used up
        if self.settings.llm.provider == "agent_sdk":
            from ..llm.agent_sdk import AgentSDKRunner
            self.sdk = AgentSDKRunner(self.settings, self.tools, self.convo, self._emit, self.provider)
            self.sdk.notify = self._send_outbound
            if self.settings.llm.api_fallback and config.secret("ANTHROPIC_API_KEY"):
                from ..llm.anthropic_provider import AnthropicProvider
                llm = self.settings.llm
                self.api_provider = AnthropicProvider(model=llm.model, effort=llm.effort, fallbacks=llm.fallbacks, thinking_display=llm.thinking_display)

    # ------------------------------------------------------------------ plumbing
    def _send_outbound(self, text: str) -> None:
        if self.outbound:
            self.outbound(text)
        else:
            log.info("outbound (no channel attached): %s", text)
        self.convo.append(self.convo.current_thread(), "assistant", [{"type": "text", "text": f"[sent via send_message] {text}"}],
                          channel="proactive")

    # ------------------------------------------------------------------ messages that arrive mid-turn
    def submit(self, text: str, channel: str = "chat", client_id: str = "", images: list[dict[str, Any]] | None = None) -> bool:
        """Called by a channel when the user sends while a turn is running: queue it and return True. The running
        turn sees it at its next step (API engine) or it starts the next turn as soon as this one ends. Returns
        False when no turn is running, so the caller should run `handle` normally."""
        with self._inbox_lock:
            if not self._turn_running:
                return False
            self._inbox.append({"text": text, "channel": channel, "client_id": client_id, "images": images or []})
            return True

    def _drain_for_turn(self) -> list[dict[str, Any]]:
        """The SDK runner polls this while a turn runs: messages the user sent meanwhile, announced to the other
        clients and the activity feed here (what the API path does inline), with image blocks inflated for the model."""
        out = self._drain_inbox()
        for m in out:
            m["inflated"] = [self.images.inflate(b) for b in m.get("images", [])]
            self._emit(kind="turn", channel=m["channel"], label=m["text"][:160], msg=None)
            self._say("me", m["text"], m["channel"], m["client_id"], images=[b["source"]["name"] for b in m.get("images", [])])
        return out

    def _drain_inbox(self) -> list[dict[str, Any]]:
        with self._inbox_lock:
            out, self._inbox = self._inbox, []
            return out

    def _say(self, who: str, text: str, channel: str, client_id: str = "", images: list[str] | None = None) -> None:
        if self.on_message is not None:
            try:
                self.on_message({"type": "message", "who": who, "text": text, "channel": channel, "client_id": client_id,
                                 **({"images": images} if images else {})})
            except Exception:  # noqa: BLE001
                log.exception("on_message hook failed")

    def _emit(self, **event: Any) -> None:
        if self.activity is not None:
            try:
                self.activity(event)
            except Exception:  # noqa: BLE001 - a display hook must never break a turn
                log.exception("activity hook failed")

    def _end_conversation(self, reason: str) -> None:
        self._ended = reason
        log.info("agent ended the conversation: %s", reason)

    def run_check(self, name: str, check_prompt: str, kind: str = "scheduled check") -> str | None:
        """A proactive turn: a configured check, a wakeup the agent scheduled for itself, or a random heartbeat.
        Returns the final text (not sent unless the model used send_message)."""
        text = f"[{kind}: {name}]\n{config.WAKEUP_RULE}\n\n{check_prompt}"
        return self.handle(text, channel="scheduler", mode="proactive", store_user=True)

    # ------------------------------------------------------------------ main entry
    def handle(self, user_text: str, channel: str = "chat", mode: str = "chat", store_user: bool = True,
               on_event: Callable[[dict[str, Any]], None] | None = None, client_id: str = "",
               images: list[dict[str, Any]] | None = None) -> str:
        """Process one message from the user (or the scheduler) and return the reply text.

        `on_event`, when given, is called as the turn unfolds, from this thread: {"type": "text", "text": delta}
        for each piece of reply text as the model writes it and {"type": "tool", "name": tool} just before a
        tool runs. The return value is still the complete reply."""
        with self._lock:
            with self._inbox_lock:
                self._turn_running = True
            try:
                reply = self._handle(user_text, channel, mode, store_user, on_event, client_id, images=images)
            finally:
                with self._inbox_lock:
                    self._turn_running = False
            if mode == "chat":
                self._say("it", reply, channel, client_id)
            # Anything the user sent after the model had already finished starts the next turn right away.
            late = self._drain_inbox()
            if late:
                text = "\n\n".join(m["text"] for m in late)
                if all(m["channel"] == "worker" for m in late):          # only worker results: a proactive turn, no reply to the user
                    self.handle("[worker results]\n" + text + "\n\nDecide what to do with these results: act on them, remember what "
                                "matters, and message the user only if they need to know now.", "worker", "proactive", True, None, "")
                else:
                    self.handle(text, late[-1]["channel"], "chat", True, None, late[-1]["client_id"],
                                images=[b for m in late for b in m.get("images", [])])
            return reply

    def run_isolated(self, task: str, thread: str, label: str, cancel: Callable[[], bool] | None = None) -> str:
        """One self-contained turn on its own conversation thread, without the main lock: what a worker runs.
        Same tools, same prompt plus the worker addendum, no user conversation."""
        text = f"[{label}]\n{task}"
        return self._handle(text, "worker", "worker", True, None, "", thread=thread, cancel=cancel)

    def _handle(self, user_text: str, channel: str, mode: str, store_user: bool,
                on_event: Callable[[dict[str, Any]], None] | None, client_id: str,
                thread: str | None = None, cancel: Callable[[], bool] | None = None,
                images: list[dict[str, Any]] | None = None) -> str:
        """`images`: stored file-reference blocks (ImageStore.save) that go in front of the user's text."""
        is_main = thread is None
        images = images or []
        if True:
            self._channel = channel
            thread = thread or self.convo.current_thread()
            if is_main and self.convo.get("ended_note"):
                user_text = ("[system note: you ended the previous conversation. The user may want to debrief or start "
                             "fresh; follow their lead.]\n" + user_text)
                self.convo.set("ended_note", None)
            user_blocks: list[dict[str, Any]] = list(images) + ([{"type": "text", "text": user_text}] if user_text.strip() or not images else [])
            user_msg_id = self.convo.append(thread, "user", user_blocks, channel) if store_user else None
            label = user_text[:160] + (f" [{len(images)} image(s)]" if images else "")
            self._emit(kind="turn", channel=channel, label=label, msg=user_msg_id)
            if mode == "chat" and store_user:
                self._say("me", user_text, channel, client_id, images=[b["source"]["name"] for b in images])
            provider = self.provider
            # Fast mode when, and only when, the user is waiting on this reply: a chat or voice turn. Checks,
            # wakeups, heartbeats, ingestion and workers think at standard speed (and standard price).
            fast = self.settings.llm.fast_when_chatting and mode in ("chat", "voice")
            if self.sdk is not None:                         # the Agent SDK runs the whole turn...
                if self.sdk.use_subscription_now():
                    out = self._sdk_turn(thread, user_text, user_msg_id, channel, mode, on_event, isolated=not is_main,
                                         images=[self.images.inflate(b) for b in images], fast=fast)
                    if out is not None:
                        return out
                if self.api_provider is None:                # ...unless the subscription is used up and the API is available
                    return "(model error: the subscription window is used up and no API key is configured for the fallback)"
                log.info("turn over the API (subscription fallback until %s)", self.sdk.describe()["api_fallback_until"])
                provider = self.api_provider
            system = prompt.build_system(self.settings)
            if mode == "worker":
                system = system + [{"type": "text", "text": WORKER_ADDENDUM}]
            messages = self.images.inflate_messages(self.convo.history(thread, self.settings.llm.history_messages))
            if not messages:
                messages = [{"role": "user", "content": self.images.inflate_messages([{"role": "user", "content": user_blocks}])[0]["content"]}]
            final_text = ""
            answered = False
            for _round in range(self.settings.llm.max_tool_rounds):
                if cancel is not None and cancel():
                    final_text = "(cancelled)"
                    break
                volatile = prompt.volatile_message(self.settings, self.memory, self.convo, channel, mode)
                extra: dict[str, Any] = {}
                if on_event is not None:
                    extra["on_text"] = lambda t: on_event({"type": "text", "text": t})
                if fast and getattr(provider, "supports_fast", False):
                    extra["fast"] = True
                try:
                    resp = provider.complete(system=system, messages=messages + [volatile], tools=self.tools.specs(),
                                             max_tokens=self.settings.llm.max_tokens, **extra)
                except LLMError as e:
                    log.error("LLM error: %s", e)
                    if not answered and user_msg_id is not None:
                        self.convo.delete(user_msg_id)      # otherwise the next turn answers this one instead
                    return f"(model error: {e})"
                except Exception:
                    if not answered and user_msg_id is not None:
                        self.convo.delete(user_msg_id)
                    raise
                answered = True
                usage.record(self.convo, engine=getattr(provider, "name", ""), billing="api", model=resp.model or getattr(provider, "model", ""),
                             usage=resp.usage, mode=mode, channel=channel, thread=thread)
                self.convo.append(thread, "assistant", resp.content, channel)
                messages.append({"role": "assistant", "content": resp.content})
                contacted = contact.check(model=resp.model or getattr(provider, "model", ""), system=system, messages=messages[:-1] + [volatile],
                                          content=resp.content, tools=self.tools.specs(), stop_reason=resp.stop_reason,
                                          extra={"channel": channel, "mode": mode, "thread": thread})
                is_final = contacted is not None or resp.stop_reason != "tool_use" or not resp.tool_uses
                for b in resp.content:
                    if b.get("type") == "thinking" and b.get("thinking"):
                        self._emit(kind="thinking", text=b["thinking"])
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        self._emit(kind="text", text=b["text"], channel=channel, final=is_final)
                    elif b.get("type") == "tool_use":
                        self._emit(kind="tool_call", id=b.get("id"), name=b.get("name"), input=b.get("input") or {})
                if resp.text:
                    final_text = resp.text
                if contacted is not None:
                    # The model asked to talk to Eli: recorded and he is notified. The task ends here, no retry;
                    # the pending tool_use blocks get results so the stored history stays well-formed.
                    paired = contact.tool_results_for(resp.content)
                    if paired:
                        self.convo.append(thread, "user", paired, channel)
                    final_text = contact.stopped_text(contacted)
                    break
                if resp.stop_reason != "tool_use" or not resp.tool_uses:
                    break
                results = []
                for tu in resp.tool_uses:
                    if on_event is not None:
                        on_event({"type": "tool", "name": tu["name"]})
                    out, err = self.tools.dispatch(tu["name"], tu.get("input") or {})
                    log.info("tool %s(%s) -> %s%s", tu["name"], _short(tu.get("input")), "ERROR " if err else "", _short(out, 160))
                    self._emit(kind="tool_result", id=tu["id"], name=tu["name"], output=_clip(out, 1500), error=bool(err))
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out, "is_error": err})
                for m in (self._drain_inbox() if is_main else []):  # the user wrote while you were working: hand it over now
                    for b in m.get("images", []):
                        results.append(b)
                    results.append({"type": "text", "text": MID_TURN_PREFIX + m["text"]})
                    self._emit(kind="turn", channel=m["channel"], label=m["text"][:160], msg=None)
                    self._say("me", m["text"], m["channel"], m["client_id"])
                    log.info("mid-turn message folded in: %s", _short(m["text"]))
                    if m["channel"] != "worker":             # the user is now waiting on what was a background turn
                        fast = self.settings.llm.fast_when_chatting
                self.convo.append(thread, "user", results, channel)
                messages.append({"role": "user", "content": self.images.inflate_messages([{"role": "user", "content": results}])[0]["content"]})
                if any(tu["name"] == "no_action_needed" for tu in resp.tool_uses):
                    # Terminal by definition: the check is over. Otherwise the model can keep re-declaring it until
                    # the round limit (seen 2026-11-06: 14 wasted calls after the work was done).
                    final_text = final_text or next((tu.get("input", {}).get("reason", "") for tu in resp.tool_uses if tu["name"] == "no_action_needed"), "")
                    break
            else:
                final_text = final_text or "(stopped after too many tool calls in one turn)"
            if self._ended and is_main:
                self.convo.new_thread()
                self.convo.set("ended_note", self._ended)
                self._ended = None
            return final_text or "(no reply)"


    def _sdk_turn(self, thread: str, user_text: str, user_msg_id: int | None, channel: str, mode: str,
                  on_event: Callable[[dict[str, Any]], None] | None, isolated: bool = False,
                  images: list[dict[str, Any]] | None = None, fast: bool = False) -> str | None:
        """One turn through the Agent SDK. Returns None when the subscription is rate-limited and the caller
        should redo the turn over the API. `isolated`: a worker, on a runner of its own (fresh session).
        `fast`: fast mode for the whole turn (the user is waiting on it)."""
        from ..llm.agent_sdk import AgentSDKRunner, ContactedEli, RateLimited
        # Only the stable block is the system prompt (a resumed SDK session keeps it); everything that changes
        # between turns rides in front of the user's message as the situation.
        system_text = prompt.stable_block(self.settings)
        if mode == "worker":
            system_text += "\n\n" + WORKER_ADDENDUM
        situation = (prompt.volatile_block(self.settings, self.memory, self.convo, channel, mode)
                     + "\n\n# ENGINE\n" + self.sdk.prompt_line())
        runner = self.sdk
        if isolated:
            runner = AgentSDKRunner(self.settings, self.tools, self.convo, self._emit, self.provider)
            runner.notify = self.sdk.notify
            self.convo.set(f"sdk_session:{thread}", None)        # a worker never resumes
        try:
            final = runner.run_turn(user_text, system_text, thread, channel, mode, on_event, images=images,
                                    drain=None if isolated else self._drain_for_turn, context=thread if isolated else "main",
                                    situation=situation, fast=fast)
        except RateLimited as e:
            log.warning("agent sdk: %s; redoing the turn over the API", e)
            return None if self.api_provider is not None else f"(model error: {e})"
        except ContactedEli as e:                 # recorded and notified; the task stops here, no retry
            return contact.stopped_text(e.record)
        except LLMError as e:
            log.error("agent sdk error: %s", e)
            if user_msg_id is not None:
                self.convo.delete(user_msg_id)
            return f"(model error: {e})"
        if self._ended:
            self.convo.new_thread()
            self.convo.set("ended_note", self._ended)
            self._ended = None
        return final or "(no reply)"


def _short(x: Any, n: int = 100) -> str:
    s = str(x).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"
