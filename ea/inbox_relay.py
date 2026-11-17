"""On a server, LLM-inbox records are written to the server's disk, but Eli reads the inbox on his Mac
(`llm-inbox`, the page on :8798). This sends each new record to the Mac through the bridge (POST /inbox).

The server notifies Eli itself the moment a record is made (ntfy / email, shared/llm_contact.py), so nothing waits
on this; the relay is what lets him open the full context and debrief. It is called right after a record is
written and again from the scheduler, so a record made while the Mac was asleep arrives when it wakes. How far it
got is a byte offset in `relay.json` beside the inbox file; the bridge ignores an id it already has.

Does nothing when the agent runs on the Mac (no `sources.bridge_url`).
"""
from __future__ import annotations

import json
import logging
import threading

from . import config
from .connectors.http import ServiceRouter, SourceError

log = logging.getLogger(__name__)
_LOCK = threading.Lock()


def forward(settings: config.Settings | None = None, router: ServiceRouter | None = None) -> int:
    """Send the records not yet sent, oldest first, stopping at the first failure. Returns how many went."""
    from .contact import llm_contact
    router = router or ServiceRouter(settings)
    path = llm_contact.MESSAGES_PATH
    if not router.bridge or not path.exists():
        return 0
    mark = path.with_name("relay.json")
    sent = 0
    with _LOCK:
        try:
            offset = int(json.loads(mark.read_text())["offset"])
        except (OSError, ValueError, KeyError, TypeError):
            offset = 0
        if offset >= path.stat().st_size:
            return 0
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line.endswith(b"\n"):            # nothing left, or a line still being written
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    rec = None
                if isinstance(rec, dict) and rec.get("type") == "contact":
                    try:
                        router.mac("POST", "/inbox", rec)
                    except SourceError as e:
                        log.info("inbox relay: %s not sent yet (%s)", rec.get("id"), str(e)[:160])
                        break
                    sent += 1
                    log.warning("inbox relay: %s is now in the inbox on the Mac", rec.get("id"))
                offset = f.tell()
                mark.write_text(json.dumps({"offset": offset}))
    return sent
