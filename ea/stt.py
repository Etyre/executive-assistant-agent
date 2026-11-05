"""Speech to text for voice notes (Telegram). The iPhone Shortcut dictates on-device, so this is only
needed for audio files. Providers: openai (whisper-1 / gpt-4o-transcribe), deepgram, local (faster-whisper
if installed, for the Mac mini), or off. `auto` picks the first one that is configured."""
from __future__ import annotations

import logging
import pathlib

import httpx

from . import config

log = logging.getLogger(__name__)


class STTError(RuntimeError):
    pass


def provider_name(settings: config.Settings | None = None) -> str:
    s = settings or config.load()
    p = s.channels.stt_provider
    if p != "auto":
        return p
    if config.secret("OPENAI_API_KEY"):
        return "openai"
    if config.secret("DEEPGRAM_API_KEY"):
        return "deepgram"
    try:
        import faster_whisper  # noqa: F401
        return "local"
    except ImportError:
        return "off"


def transcribe(path: pathlib.Path, settings: config.Settings | None = None) -> str:
    p = provider_name(settings)
    if p == "openai":
        key = config.secret("OPENAI_API_KEY")
        with path.open("rb") as f:
            r = httpx.post("https://api.openai.com/v1/audio/transcriptions", headers={"Authorization": f"Bearer {key}"},
                           files={"file": (path.name, f)}, data={"model": "gpt-4o-mini-transcribe"}, timeout=120)
        if r.status_code >= 400:
            raise STTError(f"OpenAI transcription {r.status_code}: {r.text[:200]}")
        return r.json().get("text", "").strip()
    if p == "deepgram":
        key = config.secret("DEEPGRAM_API_KEY")
        r = httpx.post("https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true",
                       headers={"Authorization": f"Token {key}", "Content-Type": "audio/ogg"}, content=path.read_bytes(), timeout=120)
        if r.status_code >= 400:
            raise STTError(f"Deepgram {r.status_code}: {r.text[:200]}")
        return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    if p == "local":
        from faster_whisper import WhisperModel  # type: ignore
        model = WhisperModel("small", compute_type="int8")
        segments, _ = model.transcribe(str(path))
        return " ".join(s.text for s in segments).strip()
    raise STTError("no speech-to-text provider configured (set OPENAI_API_KEY or DEEPGRAM_API_KEY, or pip install faster-whisper)")
