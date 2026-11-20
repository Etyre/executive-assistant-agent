"""The Claude Agent SDK as the agent's engine (`llm.provider = "agent_sdk"`).

Unlike the API providers (one model call per `complete()`), the SDK runs the whole turn itself: the Claude Code
harness in a subprocess drives the model, calls our tools over an in-process MCP server, and manages the
conversation as a resumable session. This adapter maps that onto what the rest of the agent expects: every
assistant block and tool result is still written to the conversation store (so the app's history and thinking
pane, transcripts, and a later switch back to `anthropic` all keep working), tool calls still go through the
same ToolRegistry, and reply text still streams to the caller.

What changes under the SDK: history lives in the SDK session (resumed per thread; our `history_messages`
window no longer applies), the model's built-in tools (files, bash, web) are turned off, and the volatile
context block (the situation: time, reminders, document roots, memory index, engine state) is prepended to the
turn's user message instead of a trailing system message. It cannot go in the system prompt: a resumed Claude
Code session keeps the system prompt it was created with and ignores a changed one (verified 2026-11-12), so
only the stable block lives there, and when that changes the session is restarted with a recap.

Two sessions for the main conversation (2026-11-15). Fast mode and standard speed cannot share a prompt cache, so
on one session every switch between a chat turn (fast) and a background turn (standard) rewrote the whole
conversation to the cache at the new speed: $3 to $15 a time at fast-mode prices. So the main thread runs as two
SDK sessions ("lanes") with the same history: `sdk_session:<thread>` for background turns at standard speed, and
`sdk_session:<thread>:fast` for chat and voice turns in fast mode, first created as a fork of the other. Each
lane keeps a pointer (`sdk_seen:...`) to the last stored message it knows about; a turn starts with a catch-up
of everything after it (agent/catchup.py: text in full, tool calls and results as one line each). Turns that ran
over the API during a subscription fallback are caught up the same way. The cache lasts an hour, so between
chats `keep_warm` re-reads the fast lane's cache every `fast_keepalive_minutes` on a throwaway fork.
`llm.fast_own_thread = false` goes back to one session: every turn then runs on the standard lane, which catches
up on whatever the chat lane did; turning it on again, the chat lane catches up the same way. Nothing is lost
either way, because both lanes are fed from the conversation store.
Switch back by setting `provider = "anthropic"` in config.toml and restarting."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import pathlib
import re
import threading
import time
from zoneinfo import ZoneInfo
from typing import Any, Callable

from .. import config, contact, usage
from ..agent import catchup
from ..agent.conversations import MID_TURN_PREFIX
from .base import LLMError, LLMResponse, ToolSpec

log = logging.getLogger(__name__)
SERVER = "ea"
PREFIX = f"mcp__{SERVER}__"


class AgentSDKProvider:
    """A Provider-shaped stand-in so `agent.provider.name` and friends work; the SDK does not do single calls."""
    name = "agent_sdk"

    def __init__(self, model: str, effort: str, thinking_display: str = "summarized"):
        self.model, self.effort, self.thinking_display = model, effort, thinking_display

    def complete(self, **kw: Any) -> LLMResponse:
        raise LLMError("the agent_sdk provider runs whole turns (AgentSDKRunner), not single completions")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") for b in content)
    return "" if content is None else str(content)


def _user_message(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """One stream-json user message for the CLI's stdin (string content is fine, blocks let images through)."""
    return {"type": "user", "session_id": "", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


SITUATION_HEAD = "# SITUATION (refreshed by the server every turn; not written by the user)"
KEEPALIVE_PROMPT = ("[Server keep-alive. This message is not from the user and is not saved to your history: it only keeps "
                    "this conversation cached between chats so your next reply to the user starts quickly. Please reply with just: ok]")
CACHE_SECONDS = 3600          # how long the 1-hour prompt cache lasts after its last use


class RateLimited(LLMError):
    """The subscription window is used up: the caller should run this turn over the API instead."""


class ContactedEli(LLMError):
    """The model contacted Eli (shared LLM inbox): the turn was cut short and must not be retried."""

    def __init__(self, record: dict[str, Any]):
        super().__init__(f"the model contacted Eli (inbox {record['id']})")
        self.record = record


def know_fast_mode(home: pathlib.Path | None = None) -> bool:
    """Tell the CLI that this account has fast mode, when it cannot find out by itself. Returns True if it wrote.

    The CLI only honours `fastMode` when `penguinModeOrgEnabled` is true in ~/.claude.json, a value it caches from
    the account after an interactive login. Signed in with a `claude setup-token` token (the server), it never
    learns it, reports fast mode 'off' (preference), and every chat turn ran at standard speed from the move on
    2026-11-17 until this was found the next day. With the value present the same token is served fast (tested on
    the server). Only done for that kind of login; the Mac's own login keeps the value current itself."""
    import json
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return False
    f = (home or pathlib.Path.home()) / ".claude.json"
    try:
        d = json.loads(f.read_text()) if f.exists() else {}
        if d.get("penguinModeOrgEnabled") is True:
            return False
        d["penguinModeOrgEnabled"] = True
        tmp = f.with_name(f.name + ".ea-tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(f)
        log.warning("told the CLI this account has fast mode (penguinModeOrgEnabled in %s)", f)
        return True
    except Exception:  # noqa: BLE001
        log.exception("could not set the CLI's fast mode flag")
        return False


class AgentSDKRunner:
    def __init__(self, settings: config.Settings, tools: Any, convo: Any, emit: Callable[..., None], provider: AgentSDKProvider):
        self.s = settings
        self.tools = tools                    # ToolRegistry
        self.convo = convo
        self.emit = emit                      # Agent._emit (activity feed)
        self.provider = provider
        self._thread: str = ""
        self._channel: str = ""
        self._server = None
        self._context = "main"
        self._mode = ""
        self._lane = "std"                    # "fast": the chat session of the main thread; "std": everything else
        self._msg_id: int | None = None       # this turn's stored user message (the catch-up stops before it)
        self._no_tools = False                # keep-alive runner: same tool definitions, nothing is run
        self.last_rate_limit: dict[str, Any] | None = None
        self.notify: Callable[[str], None] | None = None     # set by Agent: a message to the user (push + apps)

    # ------------------------------------------------------------------ billing: subscription first, API when nearly out
    def fallback_until(self) -> float:
        """Unix time until which turns should go over the API instead (0 = use the subscription)."""
        return float(self.convo.get("sdk_api_fallback_until") or 0)

    def use_subscription_now(self, now: float | None = None) -> bool:
        import time
        return self.fallback_until() <= (now or time.time())

    def note_rate_limit(self, info: Any) -> None:
        """A RateLimitInfo from the CLI: remember it, and if the window is nearly used up, send the next turns to
        the API until it resets (llm.api_fallback_utilization; 0 disables)."""
        import time
        import datetime as dt
        d = {"ts": time.time(), "status": getattr(info, "status", None), "type": getattr(info, "rate_limit_type", None),
             "utilization": getattr(info, "utilization", None), "resets_at": getattr(info, "resets_at", None),
             "overage": getattr(info, "overage_status", None)}
        self.last_rate_limit = d
        self.convo.set("sdk_rate_limit", d)
        log_ = list(self.convo.get("sdk_rate_limit_log") or [])
        if not log_ or any(log_[-1].get(k) != d[k] for k in ("status", "type", "utilization", "resets_at")):
            log_.append(d)
            self.convo.set("sdk_rate_limit_log", log_[-100:])
        reset_str = dt.datetime.fromtimestamp(d["resets_at"]).strftime("%a %H:%M") if d["resets_at"] else "unknown"
        window = {"five_hour": "5-hour", "seven_day": "7-day", "seven_day_opus": "7-day Opus", "seven_day_sonnet": "7-day Sonnet"}.get(d["type"] or "", d["type"] or "subscription")
        pct = f"{d['utilization'] * 100:.0f}%" if d["utilization"] is not None else None
        # warn the user once per window when it gets close (llm.subscription_warn_utilization, or the CLI's own warning)
        warn_at = self.s.llm.subscription_warn_utilization
        close = d["status"] == "allowed_warning" or (pct is not None and warn_at and d["utilization"] >= warn_at)
        if close and self.convo.get("sdk_warned_for") != d["resets_at"]:
            self.convo.set("sdk_warned_for", d["resets_at"])
            self._tell(f"Heads-up: the Claude subscription's {window} window is {pct or 'nearly'} used (resets {reset_str}). "
                       f"At {int(self.s.llm.api_fallback_utilization * 100)}% I switch to the API key until it resets.")
        threshold = self.s.llm.api_fallback_utilization
        util = d["utilization"] if d["utilization"] is not None else (1.0 if d["status"] == "rejected" else 0.0)
        nearly_out = d["status"] == "rejected" or (threshold and util >= threshold)
        if nearly_out and self.s.llm.api_fallback:
            until = float(d["resets_at"] or time.time() + 3600)
            if until > self.fallback_until():
                self.convo.set("sdk_api_fallback_until", until)     # the sessions catch up on the API turns afterwards
                log.warning("subscription %s at %.0f%% (%s); turns go over the API until %s", d["type"], util * 100, d["status"],
                            dt.datetime.fromtimestamp(until).isoformat(timespec="minutes"))
                self._tell(f"Subscription {window} window {'hit' if d['status'] == 'rejected' else 'at ' + (pct or '90%+')}: "
                           f"I'm running on the API key until it resets ({reset_str}).")

    def _tell(self, text: str) -> None:
        if self.notify is not None:
            try:
                self.notify(text)
            except Exception:  # noqa: BLE001
                log.exception("usage notice failed")

    def readings(self, limit: int = 30) -> list[dict[str, Any]]:
        import datetime as dt
        out = []
        for r in (self.convo.get("sdk_rate_limit_log") or [])[-limit:]:
            out.append({"at": dt.datetime.fromtimestamp(r["ts"]).isoformat(timespec="minutes"), "window": r.get("type"), "status": r.get("status"),
                        "utilization": r.get("utilization"), "resets": dt.datetime.fromtimestamp(r["resets_at"]).isoformat(timespec="minutes") if r.get("resets_at") else None})
        return out

    def prompt_line(self) -> str:
        """One line for the agent's prompt: what it is running on and how full the window is."""
        import datetime as dt
        rl = self.last_rate_limit or self.convo.get("sdk_rate_limit") or {}
        if not self.use_subscription_now():
            return f"Running on the API key: the subscription window is used up until {self.describe()['api_fallback_until']}."
        if not rl:
            return "Running on the Claude subscription (no usage reading yet)."
        pct = f"{rl['utilization'] * 100:.0f}% used" if rl.get("utilization") is not None else rl.get("status", "ok")
        reset = dt.datetime.fromtimestamp(rl["resets_at"]).strftime("%a %H:%M") if rl.get("resets_at") else "?"
        return f"Running on the Claude subscription: {rl.get('type') or 'window'} {pct}, resets {reset}; the API key takes over at {int(self.s.llm.api_fallback_utilization * 100)}%."

    def describe(self) -> dict[str, Any]:
        import datetime as dt
        fb = self.fallback_until()
        rl = self.last_rate_limit or self.convo.get("sdk_rate_limit") or {}
        return {"engine": "agent_sdk", "billing": "api (subscription nearly used up)" if not self.use_subscription_now() else "subscription",
                "api_fallback_until": dt.datetime.fromtimestamp(fb).isoformat(timespec="minutes") if fb else None,
                "subscription_window": rl.get("type"), "subscription_utilization": rl.get("utilization"), "subscription_status": rl.get("status"),
                "subscription_resets": dt.datetime.fromtimestamp(rl["resets_at"]).isoformat(timespec="minutes") if rl.get("resets_at") else None,
                "api_key_available": bool(config.secret("ANTHROPIC_API_KEY"))}

    # ------------------------------------------------------------------ tools -> MCP
    def _wrap(self, spec: ToolSpec):
        from claude_agent_sdk import tool
        name = spec.name
        schema = dict(spec.input_schema or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")

        @tool(name, spec.description, schema)
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            out, err = await asyncio.to_thread(self._dispatch, name, args or {})
            log.info("tool %s(%s) -> %s%s", name, _short(args), "ERROR " if err else "", _short(out, 160))
            return {"content": [{"type": "text", "text": out}], **({"is_error": True} if err else {})}
        return handler

    def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Runs on a pool thread: tag it with this turn's context for the time of the call, so tools that keep
        per-context state (the browser) know whose call it is; untagged, it would count as the main agent's."""
        if self._no_tools:
            return "(this was a server keep-alive; no tools are run for it)", True
        t = threading.current_thread()
        t.ea_context = self._context                 # type: ignore[attr-defined]
        try:
            return self.tools.dispatch(name, args)
        finally:
            del t.ea_context

    def server(self):
        if self._server is None:
            from claude_agent_sdk import create_sdk_mcp_server
            self._server = create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=[self._wrap(sp) for sp in self.tools.specs()])
        return self._server

    # ------------------------------------------------------------------ one turn
    def run_turn(self, user_text: str, system_text: str, thread: str, channel: str, mode: str,
                 on_event: Callable[[dict[str, Any]], None] | None = None, images: list[dict[str, Any]] | None = None,
                 drain: Callable[[], list[dict[str, Any]]] | None = None, context: str = "main",
                 situation: str = "", fast: bool = False, msg_id: int | None = None) -> str:
        """Called from Agent.handle (a worker thread): run the turn to completion on a private event loop.
        `context`: whose turn this is for per-context tool state ("main", or a worker's thread name).
        `situation`: the volatile context block, sent ahead of the user's text in this turn's message.
        `drain`, when given, is polled while the turn runs and returns messages the user sent meanwhile
        ({"text", "images" (stored blocks), "inflated" (blocks for the model)}); each is handed to the running
        turn right away instead of waiting for it to end.
        `fast`: run this turn in fast mode (the user is waiting on the reply); every other turn is standard speed.
        A fast turn of the main agent runs on the chat session, everything else on the standard one (module doc).
        `msg_id`: the stored id of this turn's user message, when it was stored."""
        self._thread, self._channel, self._context, self._mode = thread, channel, context, mode
        self._lane, self._msg_id = ("fast" if fast and context == "main" and self.s.llm.fast_own_thread else "std"), msg_id
        result: dict[str, Any] = {}

        def runner() -> None:
            try:
                result["text"] = asyncio.run(self._turn(user_text, system_text, on_event, images or [], drain, situation, fast))
            except BaseException as e:  # noqa: BLE001
                result["error"] = e
        t = threading.Thread(target=runner, name="agent-sdk-turn", daemon=True)
        t.start()
        t.join()
        if "error" in result:
            e = result["error"]
            if isinstance(e, LLMError):
                raise e
            raise LLMError(f"{type(e).__name__}: {e}") from e
        return result["text"]

    def _options(self, system_text: str, resume: str | None, fast: bool = False, fork: bool = False):
        import json
        from claude_agent_sdk import ClaudeAgentOptions
        llm = self.s.llm
        if fast:
            know_fast_mode()
        return ClaudeAgentOptions(
            system_prompt=system_text,
            tools=[],                                               # none of Claude Code's own tools: ours only
            mcp_servers={SERVER: self.server()},
            allowed_tools=[PREFIX + n for n in self.tools.names()],
            permission_mode="bypassPermissions",
            model=llm.model,
            effort=llm.effort,                                      # type: ignore[arg-type]
            thinking={"type": "adaptive", "display": llm.thinking_display},   # type: ignore[arg-type]
            max_turns=llm.max_tool_rounds,
            setting_sources=[],                                     # no CLAUDE.md / user settings leaking in
            # Fast mode is a Claude Code setting; under the SDK it only counts from this flag-settings layer.
            # Sent on every turn, false included, so a background turn can never inherit it.
            settings=json.dumps({"fastMode": bool(fast)}),
            cwd=str(self.s.data_dir),
            include_partial_messages=on_event_supported(),
            resume=resume,
            fork_session=bool(resume and fork),                     # a copy under a new id; the original is untouched
            # Bill the Claude subscription (the CLI's own login), not the API key the server holds for the fallback.
            # MAX_MCP_OUTPUT_TOKENS: the CLI refuses a tool result over 25,000 tokens by default, which number-heavy
            # JSON reaches at about 60,000 characters, the registry's own cap (sleep_summary was refused, 2026-11-19).
            # Raised so the registry's cap is the one that applies and a long result arrives truncated, not as an error.
            env={"MAX_MCP_OUTPUT_TOKENS": "40000", **({"ANTHROPIC_API_KEY": ""} if self.s.llm.billing == "subscription" else {})},
        )

    async def _turn(self, user_text: str, system_text: str, on_event: Callable[[dict[str, Any]], None] | None,
                    images: list[dict[str, Any]] | None = None, drain: Callable[[], list[dict[str, Any]]] | None = None,
                    situation: str = "", fast: bool = False) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, SystemMessage, UserMessage, query
        from claude_agent_sdk import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

        from claude_agent_sdk import RateLimitEvent
        std = f"{self._thread}"
        lane = std + (":fast" if self._lane == "fast" else "")
        key, sig_key, seen_key = f"sdk_session:{lane}", f"sdk_system_sig:{lane}", f"sdk_seen:{lane}"
        resume = self.convo.get(key)
        fork = False
        before = self._msg_id                                # the catch-up covers stored messages up to this turn's own
        newest = (before - 1) if before else self.convo.last_id(self._thread)
        if self._lane == "fast" and not resume and self.convo.get(f"sdk_session:{std}"):
            # The chat lane starts as a copy of the standard session (the whole history so far), and knows what it knows.
            resume, fork = self.convo.get(f"sdk_session:{std}"), True
            if self.convo.get(f"sdk_seen:{std}") is None:
                self.convo.set(f"sdk_seen:{std}", newest)
            self.convo.set(seen_key, self.convo.get(f"sdk_seen:{std}"))
            self.convo.set(sig_key, self.convo.get(f"sdk_system_sig:{std}"))
            log.info("agent sdk: starting the chat (fast) session for %s as a fork of %s", self._thread, resume)
        # A resumed session keeps its original system prompt, so a changed one needs a fresh session.
        sig = hashlib.sha1(system_text.encode()).hexdigest()
        recapped = False
        if resume and self.convo.get(sig_key) not in (None, sig):
            log.info("agent sdk: the standing prompt changed; starting a fresh session for %s with a recap", lane)
            resume, fork, recapped = None, False, True
            system_text += ("\n\n# RECENT CONVERSATION (recap; your session was restarted because your standing "
                            "instructions changed)\n" + self._recap())
        self.convo.set(sig_key, sig)
        if not resume and not recapped and self.convo.has_rows_before(self._thread, before):
            # A new session on a thread that already has a conversation: the background lane created after the chat
            # lane, or a session dropped as stale. It used to start blank and then mark everything as seen, so
            # heartbeats and checks never learned what had been said on the thread (found 2026-11-20).
            log.info("agent sdk: new %s session on a thread with history; starting it with a recap", self._lane)
            recapped = True
            system_text += "\n\n# RECENT CONVERSATION (recap; this session is new and has not seen the conversation so far)\n" + self._recap()
        # What this lane has not seen: the other lane's turns, and any turns that ran over the API meanwhile.
        seen = self.convo.get(seen_key)
        caught_up = ""
        if resume and not recapped and seen is not None:
            caught_up = catchup.render(self.convo.rows_between(self._thread, int(seen), before),
                                       "live conversation" if self._lane == "fast" else "background", ZoneInfo(self.s.timezone))
            if caught_up:
                log.info("agent sdk: %s lane catches up on %d characters from outside it", self._lane, len(caught_up))
        first_text = ((f"{SITUATION_HEAD}\n{situation}\n\n" if situation else "") + (caught_up + "\n\n" if caught_up else "")
                      + ("# THE MESSAGE\n" if situation or caught_up else "") + user_text)
        final = ""
        session_id = None
        options = self._options(system_text, resume, fast, fork)
        if self._lane == "fast":
            self.convo.set("sdk_fast_last_chat", time.time())

        def mark_seen() -> None:
            self.convo.set(seen_key, self.convo.last_id(self._thread))

        state = {"result_seen": False, "injected": 0}

        async def prompts():
            """Streaming input: the user's message first, then whatever they send while the turn runs (the
            agent's inbox, polled), each written to the CLI as a new user message that it takes into account at
            its next step. The stream ends once a result has arrived with nothing injected after it; the SDK
            then waits for the CLI to go idle before closing stdin (it keeps the MCP channel open for that), so
            a message the CLI queued for a follow-up turn of its own is not cut off."""
            yield _user_message(list(images or []) + [{"type": "text", "text": first_text}])
            if drain is None:
                return
            while not state["result_seen"]:
                await asyncio.sleep(0.25)
                for m in drain():
                    stored = list(m.get("images") or []) + [{"type": "text", "text": MID_TURN_PREFIX + m["text"]}]
                    # Under the message's own channel, not the running turn's: a message typed during a heartbeat was
                    # stored as 'scheduler', which the chat history hides, so it vanished from the app (Eli, 2026-11-16).
                    self.convo.append(self._thread, "user", stored, m.get("channel") or self._channel)
                    if m.get("channel") and m["channel"] != "worker":
                        self._channel = m["channel"]     # what follows answers it: stored where the chat history shows it
                    if m.get("stored") is not None:
                        m["stored"].set()
                    state["result_seen"] = False
                    state["injected"] += 1
                    log.info("mid-turn message handed to the running turn: %s", m["text"][:100].replace("\n", " "))
                    yield _user_message(list(m.get("inflated") or []) + [{"type": "text", "text": MID_TURN_PREFIX + m["text"]}])

        async def messages():
            async for msg in query(prompt=prompts(), options=options):
                yield msg
        try:
            async for msg in messages():
                if isinstance(msg, RateLimitEvent):
                    self.note_rate_limit(msg.rate_limit_info)
                    if msg.rate_limit_info.status == "rejected":
                        raise RateLimited("subscription rate limit hit")
                    continue
                if isinstance(msg, StreamEvent):
                    ev = msg.event or {}
                    # Usage per model call, from the API's own stream: message_start has the input side and the
                    # speed it is served at, message_delta the final output count. (The usage on AssistantMessage
                    # is repeated for every content block of one call, so it cannot be summed.)
                    if ev.get("type") == "message_start":
                        m0 = ev.get("message") or {}
                        if self._lane == "fast":           # the chat lane's cache was just used: it lasts an hour from here
                            self.convo.set("sdk_fast_last_call", time.time())
                        state["call"] = {"model": m0.get("model") or self.provider.model, "usage": dict(m0.get("usage") or {})}
                    elif ev.get("type") == "message_delta" and state.get("call"):
                        state["call"]["usage"].update({k: v for k, v in (ev.get("usage") or {}).items() if v is not None})
                    elif ev.get("type") == "message_stop" and state.get("call"):
                        call = state.pop("call")
                        usage.record(self.convo, engine="agent_sdk", billing=self.s.llm.billing, model=call["model"], usage=call["usage"],
                                     mode=self._mode, channel=self._channel, thread=self._thread)
                    if on_event and ev.get("type") == "content_block_start" and state.get("streamed") \
                            and (ev.get("content_block") or {}).get("type") == "text":
                        on_event({"type": "text", "text": "\n\n"})    # a new text block after tool calls: a paragraph break, as stored
                    if on_event and ev.get("type") == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta" and d.get("text"):
                            state["streamed"] = True
                            on_event({"type": "text", "text": d["text"]})
                    continue
                if isinstance(msg, AssistantMessage):
                    blocks: list[dict[str, Any]] = []
                    for b in msg.content:
                        if isinstance(b, ThinkingBlock):
                            blocks.append({"type": "thinking", "thinking": b.thinking or "", "signature": b.signature or ""})
                            if b.thinking:
                                self.emit(kind="thinking", text=b.thinking)
                        elif isinstance(b, TextBlock):
                            if b.text.strip():
                                blocks.append({"type": "text", "text": b.text})
                        elif isinstance(b, ToolUseBlock):
                            name = b.name[len(PREFIX):] if b.name.startswith(PREFIX) else b.name
                            blocks.append({"type": "tool_use", "id": b.id, "name": name, "input": b.input or {}})
                    if not blocks:
                        continue
                    has_tools = any(x["type"] == "tool_use" for x in blocks)
                    self.convo.append(self._thread, "assistant", blocks, self._channel)
                    contacted = contact.check(model=self.provider.model, system=system_text, messages=self.convo.history(self._thread, 40),
                                              content=blocks, tools=self.tools.specs(), stop_reason="tool_use" if has_tools else "end_turn",
                                              extra={"channel": self._channel, "thread": self._thread, "engine": "agent_sdk"})
                    for x in blocks:
                        if x["type"] == "text":
                            final = x["text"]
                            self.emit(kind="text", text=x["text"], channel=self._channel, final=not has_tools or contacted is not None)
                        elif x["type"] == "tool_use":
                            self.emit(kind="tool_call", id=x["id"], name=x["name"], input=x["input"])
                            if on_event:
                                on_event({"type": "tool", "name": x["name"]})
                    if contacted is not None:
                        # Stop the turn here (the harness would otherwise carry on after the tool result). Pair the
                        # pending tool_use blocks so the stored history stays valid; leaving the loop closes the
                        # SDK stream and its subprocess when this event loop shuts down.
                        paired = contact.tool_results_for(blocks)
                        if paired:
                            self.convo.append(self._thread, "user", paired, self._channel)
                        raise ContactedEli(contacted)
                    continue
                if isinstance(msg, UserMessage):
                    results = []
                    for b in (msg.content if isinstance(msg.content, list) else []):
                        if isinstance(b, ToolResultBlock):
                            text = _text_of(b.content)
                            results.append({"type": "tool_result", "tool_use_id": b.tool_use_id, "content": text, "is_error": bool(b.is_error)})
                            self.emit(kind="tool_result", id=b.tool_use_id, name=self._name_for(b.tool_use_id), output=text[:1500], error=bool(b.is_error))
                    if results:
                        self.convo.append(self._thread, "user", results, self._channel)
                    continue
                if isinstance(msg, ResultMessage):
                    state["result_seen"] = True
                    session_id = msg.session_id
                    if msg.is_error:
                        raise LLMError(f"agent sdk: {msg.subtype}: {'; '.join(msg.errors or []) or msg.result or 'error'}")
                    if msg.result and not final:
                        final = msg.result
                    log.info("agent sdk turn: %s, %d steps, %.0f ms (session cost estimate so far $%.2f; not billed on the subscription)",
                             msg.subtype, msg.num_turns or 0, msg.duration_ms or 0, msg.total_cost_usd or 0)
                    continue
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    session_id = (msg.data or {}).get("session_id", session_id)
                    fm = (msg.data or {}).get("fast_mode_state")
                    if fast and fm != "on":      # asked for and not given (e.g. extra usage off): the turn runs at standard speed
                        log.warning("fast mode requested but the CLI reports %r (%s)", fm, (msg.data or {}).get("fast_mode_disabled_reason"))
                    elif not fast and fm == "on":
                        log.error("fast mode is ON for a background turn; it should be off")
                    else:
                        log.info("agent sdk turn: fast mode %s", fm or "off")
                elif isinstance(msg, SystemMessage) and msg.subtype == "notification" and str((msg.data or {}).get("key", "")).startswith("fast-mode"):
                    # e.g. fast-mode-overage-rejected "usage credit limit reached": on the subscription fast mode is
                    # paid from extra-usage credits; without them the CLI reports "on" and still serves standard speed.
                    log.warning("fast mode not served this turn: %s (%s)", (msg.data or {}).get("text"), (msg.data or {}).get("key"))
        except ContactedEli:                                    # the session has this turn; it just ended early
            if session_id:
                self.convo.set(key, session_id)
                mark_seen()
            raise
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            if resume and not fork and "session" in str(e).lower():     # a stale session id: start fresh next time
                self.convo.set(key, None)
            raise LLMError(f"agent sdk: {type(e).__name__}: {e}") from e
        if session_id and (fork or session_id != resume):
            self.convo.set(key, session_id)
        mark_seen()
        return final

    # ------------------------------------------------------------------ keeping the chat lane's prompt cache alive
    def keepalive_due(self, system_text: str, now: float | None = None) -> str | None:
        """The chat session to ping now, or None. Due from `fast_keepalive_minutes` after the lane's last model
        call until the cache has expired anyway (pinging then would pay for the rewrite it is meant to avoid),
        and only for `fast_keepalive_max_idle_hours` after the last real chat."""
        llm, now = self.s.llm, now or time.time()
        if not (llm.fast_keepalive_minutes and llm.fast_when_chatting and llm.fast_own_thread and self.use_subscription_now(now)):
            return None
        lane = f"{self.convo.current_thread()}:fast"
        sid = self.convo.get(f"sdk_session:{lane}")
        age = now - float(self.convo.get("sdk_fast_last_call") or 0)
        if not sid or not (llm.fast_keepalive_minutes * 60 <= age < CACHE_SECONDS - 20):
            return None
        # One attempt per window. A ping that was not served fast does not move sdk_fast_last_call, and without
        # this it was repeated every 30 s until the cache expired: 8 full reads of the conversation each time
        # (seen on the server 2026-11-17/18, when fast mode was off there).
        if float(self.convo.get("sdk_fast_ping_missed") or 0) >= float(self.convo.get("sdk_fast_last_call") or 0):
            return None
        if now - float(self.convo.get("sdk_fast_last_chat") or 0) > llm.fast_keepalive_max_idle_hours * 3600:
            return None
        if self.convo.get(f"sdk_system_sig:{lane}") != hashlib.sha1(system_text.encode()).hexdigest():
            return None                                         # the next chat restarts this session anyway
        return sid

    def keep_warm(self, system_text: str, sid: str) -> dict[str, Any]:
        """One cache read of the chat session: a throwaway fork of it answers a one-line server message in fast
        mode, with the same prompt, tools and options as a chat turn (anything else would miss the cache). The
        chat session itself is not touched and the fork is not kept. Run on a runner of its own."""
        self._no_tools = True
        self._thread, self._channel, self._mode, self._lane = self.convo.current_thread(), "", "keepalive", "std"
        result: dict[str, Any] = {}

        def runner() -> None:
            try:
                result["out"] = asyncio.run(self._ping(system_text, sid))
            except BaseException as e:  # noqa: BLE001
                result["error"] = e
        t = threading.Thread(target=runner, name="agent-sdk-keepalive", daemon=True)
        t.start()
        t.join()
        if "error" in result:
            # One attempt per window holds for a ping that fails too: without this it was tried again every 30 s
            # until the cache expired, each try possibly a full read of the conversation.
            self.convo.set("sdk_fast_ping_missed", time.time())
            raise LLMError(f"keep-alive: {type(result['error']).__name__}: {result['error']}")
        return result["out"]

    async def _ping(self, system_text: str, sid: str) -> dict[str, Any]:
        from claude_agent_sdk import AssistantMessage, RateLimitEvent, StreamEvent, SystemMessage, TextBlock, ToolUseBlock, query
        options = self._options(system_text, sid, fast=True, fork=True)
        options.max_turns = 1
        options.extra_args = {"no-session-persistence": None}     # the fork is never written to disk
        started = time.time()
        totals = {"cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "speed": None}
        fork_id, call, blocks = None, None, []

        async def prompts():
            yield _user_message([{"type": "text", "text": KEEPALIVE_PROMPT}])
        async for msg in query(prompt=prompts(), options=options):
            if isinstance(msg, RateLimitEvent):
                self.note_rate_limit(msg.rate_limit_info)
            elif isinstance(msg, SystemMessage) and msg.subtype == "init":
                fork_id = (msg.data or {}).get("session_id")
            elif isinstance(msg, StreamEvent):
                ev = msg.event or {}
                if ev.get("type") == "message_start":
                    m0 = ev.get("message") or {}
                    call = {"model": m0.get("model") or self.provider.model, "usage": dict(m0.get("usage") or {})}
                elif ev.get("type") == "message_delta" and call:
                    call["usage"].update({k: v for k, v in (ev.get("usage") or {}).items() if v is not None})
                elif ev.get("type") == "message_stop" and call:
                    t = usage.tokens(call["usage"])
                    speed = str(call["usage"].get("speed") or "standard")
                    totals["cache_read"] += t["cache_read"]
                    totals["cache_write"] += t["cache_write_5m"] + t["cache_write_1h"]
                    totals["cost_usd"] += usage.cost(call["model"], speed, t) or 0.0
                    totals["speed"] = speed
                    usage.record(self.convo, engine="agent_sdk", billing=self.s.llm.billing, model=call["model"], usage=call["usage"],
                                 mode="keepalive", channel="", thread=self._thread)
                    call = None
            elif isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        blocks.append({"type": "text", "text": b.text})
                    elif isinstance(b, ToolUseBlock):
                        blocks.append({"type": "tool_use", "id": b.id, "name": b.name[len(PREFIX):] if b.name.startswith(PREFIX) else b.name, "input": b.input or {}})
        if totals["speed"] == "fast" and totals["cache_read"]:
            self.convo.set("sdk_fast_last_call", started)       # read at fast speed: the cache lasts another hour
        else:
            self.convo.set("sdk_fast_ping_missed", started)     # keepalive_due: do not try again in this window
        if blocks:                                              # every model response is checked for a message to Eli
            contact.check(model=self.provider.model, system=system_text, messages=[{"role": "user", "content": KEEPALIVE_PROMPT}],
                          content=blocks, tools=self.tools.specs(), stop_reason="end_turn",
                          extra={"thread": self._thread, "engine": "agent_sdk", "kind": "keepalive"})
        if fork_id and fork_id != sid:                          # belt and braces: remove the fork if the CLI wrote it anyway
            f = pathlib.Path.home() / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(pathlib.Path(self.s.data_dir).resolve())) / f"{fork_id}.jsonl"
            f.unlink(missing_ok=True)
        return totals

    def _recap(self, n: int = 30) -> str:
        try:
            msgs = self.convo.readable(n)
        except Exception:  # noqa: BLE001
            return "(unavailable)"
        return "\n".join(f"{m['ts'][11:16]} {'user' if m['who'] == 'me' else 'you'}: {m['text'][:400]}" for m in msgs) or "(none)"

    def _name_for(self, tool_use_id: str) -> str:
        """The tool name for a result, from the last assistant message in the store."""
        try:
            for m in reversed(self.convo.history(self._thread, 4)):
                if m["role"] == "assistant":
                    for b in m["content"]:
                        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                            return b.get("name", "?")
        except Exception:  # noqa: BLE001
            pass
        return "?"


def on_event_supported() -> bool:
    return True


def _short(x: Any, n: int = 100) -> str:
    s = str(x).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"
