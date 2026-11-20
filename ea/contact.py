"""The shared LLM inbox (agent-coding standard, 2026-11-07): the model can reach Eli from any turn.

Every system prompt starts with `shared/llm-system-prompt-preamble.md` (see `system_prompt`), every call offers
the `contact_eli` tool, and every response is checked for a `contact_eli` call or an `@ELI:` line. When one is
found the contact is recorded in ~/Library/Application Support/LLMInbox/messages.jsonl (with the full context),
Eli is notified, and the turn stops there: no retry, no further tool rounds. Eli reads and answers with `llm-inbox`.

Every call also offers `note_to_eli` (2026-11-20), which leaves Eli a message and does NOT stop the turn: `check`
records it (once, with the same context as a contact) and returns None, and the registry handler answers the call
with the shared NOTE_RESULT_TEXT so the model carries on.

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
NOTE_TOOL = llm_contact.NOTE_TOOL
NOTE_NAME = NOTE_TOOL["name"]
NOTE_RESULT = llm_contact.NOTE_RESULT_TEXT                # what the model sees after a note: delivered, carry on


def system_prompt(instructions: str) -> str:
    """The shared preamble, word for word, followed by the agent's own instructions."""
    return llm_contact.system_prompt(instructions)


def register(reg: Any) -> None:
    """Expose `contact_eli` to the model like any other tool (both engines take tools from the registry)."""
    @reg.tool(TOOL_NAME, CONTACT_TOOL["description"], CONTACT_TOOL["input_schema"], writes=True)
    def contact_eli(message: str) -> str:
        log.warning("contact_eli called by the model: %s", message[:200])
        return TOOL_RESULT

    # Both engines run tools through the registry, so this handler is what answers a note and lets the turn go on.
    # It does not record: check() sees the same call in the response and records it there, once, with the context.
    @reg.tool(NOTE_NAME, NOTE_TOOL["description"], NOTE_TOOL["input_schema"], writes=True)
    def note_to_eli(message: str) -> str:
        log.info("note_to_eli called by the model: %s", message[:200])
        return NOTE_RESULT


def check(*, model: str, system: Any, messages: Any, content: list[dict[str, Any]], tools: Any = None,
          stop_reason: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Record and notify if the response contains a contact signal. Returns the inbox record, else None.
    On a record the caller stops the task. Never raises: a broken inbox must not take the agent down.
    `note_to_eli` calls are recorded here too (once per call, same context), but they return None: the turn goes on."""
    noted = 0
    response = {"role": "assistant", "content": content, "stop_reason": stop_reason, "model": model}
    try:
        for _, note in llm_contact.extract_notes(response):
            n = llm_contact.record(APP, model, note, "note", _plain_system(system), messages, response, _plain_tools(tools),
                                   extra={**(extra or {}), "halts": False})
            noted += 1
            log.info("the model left Eli a note (inbox %s), the turn carries on: %s", n["id"], note[:300])
    except Exception:  # noqa: BLE001
        log.exception("llm inbox note failed")
    try:
        found = llm_contact.extract(response)              # same two signals llm_contact.check() looks for
        if not found:
            if noted:
                _relay()
            return None
        message = "\n\n".join(m for _, m in found)
        via = found[0][0] if len(found) == 1 else "+".join(sorted({v for v, _ in found}))
        rec = llm_contact.record(APP, model, message, via, _plain_system(system), messages, response, _plain_tools(tools),
                                 extra=extra)              # extra: channel, mode, thread, engine for the debrief
    except Exception:  # noqa: BLE001
        log.exception("llm inbox check failed")
        return None
    log.warning("the model contacted Eli (inbox %s via %s): %s", rec["id"], rec["via"], rec["message"][:300])
    _relay()
    return rec


def _relay() -> None:
    try:                                                   # on a server: copy it to the inbox on the Mac
        from . import inbox_relay
        inbox_relay.forward()
    except Exception:  # noqa: BLE001
        log.exception("inbox relay failed; the scheduler will try again")


def stopped_text(rec: dict[str, Any]) -> str:
    """What the turn returns in place of a reply."""
    return f"(I asked to talk to Eli; recorded in the LLM inbox as {rec['id']}. This task stops here: `llm-inbox show {rec['id']}`.)"


def tool_results_for(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair every tool_use in the stopping response with a result, so the stored history stays valid for the
    next turn (the contact itself gets TOOL_RESULT, a note in the same response was recorded and gets NOTE_RESULT;
    anything else was not run)."""
    ours = {TOOL_NAME: TOOL_RESULT, NOTE_NAME: NOTE_RESULT}
    return [{"type": "tool_result", "tool_use_id": b["id"],
             "content": ours.get(b.get("name"), NOT_RUN), "is_error": b.get("name") not in ours}
            for b in content if b.get("type") == "tool_use"]


def _plain_system(system: Any) -> Any:
    if isinstance(system, list):
        return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
    return system


def _plain_tools(tools: Any) -> Any:
    if tools is None:
        return None
    return [dataclasses.asdict(t) if dataclasses.is_dataclass(t) else t for t in tools]
