"""The Claude Agent SDK as the agent's engine (`llm.provider = "agent_sdk"`).

Unlike the API providers (one model call per `complete()`), the SDK runs the whole turn itself: the Claude Code
harness in a subprocess drives the model, calls our tools over an in-process MCP server, and manages the
conversation as a resumable session. This adapter maps that onto what the rest of the agent expects: every
assistant block and tool result is still written to the conversation store (so the app's history and thinking
pane, transcripts, and a later switch back to `anthropic` all keep working), tool calls still go through the
same ToolRegistry, and reply text still streams to the caller.

What changes under the SDK: history lives in the SDK session (resumed per thread; our `history_messages`
window no longer applies), the model's built-in tools (files, bash, web) are turned off, and the volatile
context block goes into the system prompt each turn instead of a trailing system message.
Switch back by setting `provider = "anthropic"` in config.toml and restarting."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

from .. import config, contact
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


class RateLimited(LLMError):
    """The subscription window is used up: the caller should run this turn over the API instead."""


class ContactedEli(LLMError):
    """The model contacted Eli (shared LLM inbox): the turn was cut short and must not be retried."""

    def __init__(self, record: dict[str, Any]):
        super().__init__(f"the model contacted Eli (inbox {record['id']})")
        self.record = record


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
                self.convo.set("sdk_api_fallback_until", until)
                self.convo.set("sdk_resume_after_fallback", True)     # the session will miss the API turns
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
                 drain: Callable[[], list[dict[str, Any]]] | None = None, context: str = "main") -> str:
        """Called from Agent.handle (a worker thread): run the turn to completion on a private event loop.
        `context`: whose turn this is for per-context tool state ("main", or a worker's thread name).
        `drain`, when given, is polled while the turn runs and returns messages the user sent meanwhile
        ({"text", "images" (stored blocks), "inflated" (blocks for the model)}); each is handed to the running
        turn right away instead of waiting for it to end."""
        self._thread, self._channel, self._context = thread, channel, context
        result: dict[str, Any] = {}

        def runner() -> None:
            try:
                result["text"] = asyncio.run(self._turn(user_text, system_text, on_event, images or [], drain))
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

    def _options(self, system_text: str, resume: str | None):
        from claude_agent_sdk import ClaudeAgentOptions
        llm = self.s.llm
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
            cwd=str(self.s.data_dir),
            include_partial_messages=on_event_supported(),
            resume=resume,
            # Bill the Claude subscription (the CLI's own login), not the API key the server holds for the fallback.
            env={"ANTHROPIC_API_KEY": ""} if self.s.llm.billing == "subscription" else {},
        )

    async def _turn(self, user_text: str, system_text: str, on_event: Callable[[dict[str, Any]], None] | None,
                    images: list[dict[str, Any]] | None = None, drain: Callable[[], list[dict[str, Any]]] | None = None) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, SystemMessage, UserMessage, query
        from claude_agent_sdk import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

        from claude_agent_sdk import RateLimitEvent
        key = f"sdk_session:{self._thread}"
        resume = self.convo.get(key)
        if resume and self.convo.get("sdk_resume_after_fallback"):
            # Turns ran over the API meanwhile; the SDK session never saw them. Start fresh with a recap.
            resume = None
            self.convo.set("sdk_resume_after_fallback", None)
            system_text += "\n\n# RECENT CONVERSATION (recap; your session was restarted)\n" + self._recap()
        final = ""
        session_id = None
        options = self._options(system_text, resume)

        state = {"result_seen": False, "injected": 0}

        async def prompts():
            """Streaming input: the user's message first, then whatever they send while the turn runs (the
            agent's inbox, polled), each written to the CLI as a new user message that it takes into account at
            its next step. The stream ends once a result has arrived with nothing injected after it; the SDK
            then waits for the CLI to go idle before closing stdin (it keeps the MCP channel open for that), so
            a message the CLI queued for a follow-up turn of its own is not cut off."""
            yield _user_message(list(images or []) + [{"type": "text", "text": user_text}])
            if drain is None:
                return
            while not state["result_seen"]:
                await asyncio.sleep(0.25)
                for m in drain():
                    stored = list(m.get("images") or []) + [{"type": "text", "text": MID_TURN_PREFIX + m["text"]}]
                    self.convo.append(self._thread, "user", stored, self._channel)
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
                    if on_event and ev.get("type") == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta" and d.get("text"):
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
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            if resume and "session" in str(e).lower():          # a stale session id: start fresh next time
                self.convo.set(key, None)
            raise LLMError(f"agent sdk: {type(e).__name__}: {e}") from e
        if session_id and session_id != resume:
            self.convo.set(key, session_id)
        return final

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
