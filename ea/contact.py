"""The shared LLM inbox (agent-coding standard, 2026-11-07): the model can reach Eli from any turn.

Every system prompt starts with `shared/llm-system-prompt-preamble.md` (see `system_prompt`), every call offers
the `contact_eli` tool, and every response is checked for a `contact_eli` call or an `@ELI:` line. When one is
found the contact is recorded in ~/Library/Application Support/LLMInbox/messages.jsonl (with the full context),
Eli is notified, and the turn stops there: no retry, no further tool rounds. Eli reads and answers with `llm-inbox`.

The shared module lives outside this repo at /Users/elityre/Documents/agent-coding/shared (override with
EA_SHARED_DIR). A deployment without it (the Docker image) must copy that folder in and point EA_SHARED_DIR at it.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import sys
from typing import Any

SHARED_DIR = pathlib.Path(os.environ.get("EA_SHARED_DIR") or "/Users/elityre/Documents/agent-coding/shared").expanduser()
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
try:
    import llm_contact                                   # noqa: E402  (shared/llm_contact.py)
except ImportError as e:  # pragma: no cover - a misconfigured deployment should fail at import, loudly
    raise ImportError(f"shared llm_contact.py not found in {SHARED_DIR}; set EA_SHARED_DIR to the agent-coding/shared folder") from e

log = logging.getLogger(__name__)

APP = "executive-assistant"
CONTACT_TOOL = llm_contact.CONTACT_TOOL
TOOL_NAME = CONTACT_TOOL["name"]
# What the model sees as the tool's result (API path: synthesised, the task is already over; SDK path: returned by
# the MCP handler just before the turn is cut short).
TOOL_RESULT = "Recorded and sent to Eli. This task stops here; he will follow up with you."
NOT_RUN = "not run: this task stopped because you contacted Eli"


def system_prompt(instructions: str) -> str:
    """The shared preamble, word for word, followed by the agent's own instructions."""
    return llm_contact.system_prompt(instructions)


def register(reg: Any) -> None:
    """Expose `contact_eli` to the model like any other tool (both engines take tools from the registry)."""
    @reg.tool(TOOL_NAME, CONTACT_TOOL["description"], CONTACT_TOOL["input_schema"], writes=True)
    def contact_eli(message: str) -> str:
        log.warning("contact_eli called by the model: %s", message[:200])
        return TOOL_RESULT


def check(*, model: str, system: Any, messages: Any, content: list[dict[str, Any]], tools: Any = None,
          stop_reason: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Record and notify if the response contains a contact signal. Returns the inbox record, else None.
    On a record the caller stops the task. Never raises: a broken inbox must not take the agent down."""
    try:
        response = {"role": "assistant", "content": content, "stop_reason": stop_reason, "model": model}
        found = llm_contact.extract(response)              # same two signals llm_contact.check() looks for
        if not found:
            return None
        message = "\n\n".join(m for _, m in found)
        via = found[0][0] if len(found) == 1 else "+".join(sorted({v for v, _ in found}))
        rec = llm_contact.record(APP, model, message, via, _plain_system(system), messages, response, _plain_tools(tools),
                                 extra=extra)              # extra: channel, mode, thread, engine for the debrief
    except Exception:  # noqa: BLE001
        log.exception("llm inbox check failed")
        return None
    log.warning("the model contacted Eli (inbox %s via %s): %s", rec["id"], rec["via"], rec["message"][:300])
    return rec


def stopped_text(rec: dict[str, Any]) -> str:
    """What the turn returns in place of a reply."""
    return f"(I asked to talk to Eli; recorded in the LLM inbox as {rec['id']}. This task stops here: `llm-inbox show {rec['id']}`.)"


def tool_results_for(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair every tool_use in the stopping response with a result, so the stored history stays valid for the
    next turn (the contact itself gets TOOL_RESULT; anything else was not run)."""
    return [{"type": "tool_result", "tool_use_id": b["id"],
             "content": TOOL_RESULT if b.get("name") == TOOL_NAME else NOT_RUN, "is_error": b.get("name") != TOOL_NAME}
            for b in content if b.get("type") == "tool_use"]


def _plain_system(system: Any) -> Any:
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
    return system


def _plain_tools(tools: Any) -> Any:
    if tools is None:
        return None
    return [dataclasses.asdict(t) if dataclasses.is_dataclass(t) else t for t in tools]
