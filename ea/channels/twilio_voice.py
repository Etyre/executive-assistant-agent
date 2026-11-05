"""Live phone conversations through Twilio Programmable Voice, turn by turn: Twilio transcribes what the
user says (<Gather input="speech">), the agent answers in voice mode, Twilio speaks it (<Say>), and listens
again. No audio streaming to run, works with the ordinary Phone app, and the agent can also place a call
(call_user) when it has something urgent.

Every webhook is checked against Twilio's request signature and the caller against an allow-list."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any
from xml.sax.saxutils import escape

import httpx

from .. import config

log = logging.getLogger(__name__)


def valid_signature(auth_token: str, url: str, form: dict[str, Any], signature: str | None) -> bool:
    """Twilio's scheme: HMAC-SHA1 over the full URL followed by the POST params sorted by name, key+value concatenated."""
    if not signature:
        return False
    payload = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def twiml_gather(prompt_text: str, action: str, voice: str, hints: str = "") -> str:
    say = f'<Say voice="{escape(voice)}">{escape(prompt_text)}</Say>' if prompt_text else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Gather input="speech" action="{escape(action)}" method="POST" speechTimeout="auto" '
            f'speechModel="phone_call" enhanced="true" actionOnEmptyResult="true"'
            + (f' hints="{escape(hints)}"' if hints else "") + f'>{say}</Gather>'
            f'<Say voice="{escape(voice)}">Goodbye.</Say></Response>')


def twiml_say_hangup(text: str, voice: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{escape(voice)}">{escape(text)}</Say><Hangup/></Response>'


def place_call(to_number: str, settings: config.Settings | None = None) -> dict[str, Any]:
    """Outbound call: Twilio dials the user and, when they answer, fetches /twilio/voice like an inbound call."""
    s = settings or config.load()
    sid, tok = config.secret(s.channels.twilio_account_sid_env), config.secret(s.channels.twilio_auth_token_env)
    if not (sid and tok and s.channels.twilio_from_number and s.channels.public_base_url):
        raise RuntimeError("Twilio is not configured (account sid, auth token, from number, public_base_url)")
    r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", auth=(sid, tok),
                   data={"To": to_number, "From": s.channels.twilio_from_number, "Url": f"{s.channels.public_base_url.rstrip('/')}/twilio/voice?outbound=1"},
                   timeout=30)
    r.raise_for_status()
    return {"sid": r.json().get("sid"), "status": r.json().get("status")}
