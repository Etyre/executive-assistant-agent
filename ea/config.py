"""Configuration: one TOML file for preferences, environment variables (or a .env file) for secrets.

Everything that differs between the cloud deployment and the Mac mini is a URL or a token here, so the
same code runs in both places. `EA_DATA` points at the writable state directory (memory, transcripts,
SQLite); `EA_CONFIG` at the TOML file. Secrets are read from the environment first, then from
`<EA_DATA>/.env` (NAME=value lines), so a Finder- or launchd-started process finds them too.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import tomllib
from functools import lru_cache
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = pathlib.Path(os.environ.get("EA_DATA") or ROOT / "data").expanduser()
CONFIG_PATH = pathlib.Path(os.environ.get("EA_CONFIG") or ROOT / "config.toml").expanduser()
INSTRUCTIONS_DIR = pathlib.Path(os.environ.get("EA_INSTRUCTIONS") or ROOT / "instructions").expanduser()


def _load_env_file(path: pathlib.Path) -> None:
    """NAME=value lines; variables already set in the environment win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


for _p in (DATA_DIR / ".env", ROOT / ".env"):
    _load_env_file(_p)


def secret(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclasses.dataclass
class LLMSettings:
    provider: str = "anthropic"               # anthropic | openai_compat
    model: str = "claude-opus-5-5"
    effort: str = "high"                      # low | medium | high | xhigh | max (Anthropic only)
    max_tokens: int = 16000
    fallbacks: bool = True                    # Anthropic server-side refusal fallbacks (beta)
    openai_base_url: str = "http://127.0.0.1:11434/v1"   # Ollama / vLLM / LM Studio when provider=openai_compat
    openai_api_key_env: str = "OPENAI_COMPAT_API_KEY"
    max_tool_rounds: int = 20
    history_messages: int = 80                # conversation window sent to the model


@dataclasses.dataclass
class SourceSettings:
    """Where the data lives. Either a bridge (cloud deployment, reaching the Mac over Tailscale) or
    direct localhost URLs (running on the Mac itself)."""
    bridge_url: str = ""                      # e.g. http://100.x.y.z:8799  (empty = direct)
    bridge_token_env: str = "EA_BRIDGE_TOKEN"
    tracker_url: str = "http://127.0.0.1:8791"
    focus_url: str = "http://127.0.0.1:8790"
    bandit_url: str = "http://127.0.0.1:8777"
    exercise_export_url_env: str = "EXERCISE_EXPORT_URL"
    toggl_token_env: str = "TOGGL_API_TOKEN"
    tracker_agent_token_env: str = "AGENT_API_TOKEN"      # the tracker's optional agent-API bearer token
    calendar_ics_urls_env: str = "CALENDAR_ICS_URLS"      # iCal feed URL(s), comma-separated; else via the tracker
    toggl_workspace_id: int | None = None
    timeout_seconds: float = 20.0


@dataclasses.dataclass
class ChannelSettings:
    telegram_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_allowed_user_ids: list[int] = dataclasses.field(default_factory=list)
    web_host: str = "0.0.0.0"
    web_port: int = 8800
    web_token_env: str = "EA_WEB_TOKEN"       # bearer for /api/message (iOS Shortcut, web chat)
    public_base_url: str = ""                 # https://... where Twilio can reach us
    twilio_account_sid_env: str = "TWILIO_ACCOUNT_SID"
    twilio_auth_token_env: str = "TWILIO_AUTH_TOKEN"
    twilio_from_number: str = ""
    twilio_allowed_callers: list[str] = dataclasses.field(default_factory=list)
    twilio_voice: str = "Google.en-US-Chirp3-HD-Aoede"
    stt_provider: str = "auto"                # auto | openai | deepgram | local | off


@dataclasses.dataclass
class CheckSettings:
    name: str
    time: str                                 # "HH:MM" local
    prompt: str
    days: list[str] = dataclasses.field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])


@dataclasses.dataclass
class Settings:
    agent_name: str = "Atlas"
    user_name: str = "Eli"
    timezone: str = "America/Los_Angeles"
    llm: LLMSettings = dataclasses.field(default_factory=LLMSettings)
    sources: SourceSettings = dataclasses.field(default_factory=SourceSettings)
    channels: ChannelSettings = dataclasses.field(default_factory=ChannelSettings)
    checks: list[CheckSettings] = dataclasses.field(default_factory=list)
    reminder_poll_seconds: int = 30

    # ----- derived
    @property
    def data_dir(self) -> pathlib.Path:
        return DATA_DIR

    @property
    def memory_dir(self) -> pathlib.Path:
        return DATA_DIR / "memory"

    @property
    def transcripts_dir(self) -> pathlib.Path:
        return DATA_DIR / "transcripts"

    @property
    def state_db(self) -> pathlib.Path:
        return DATA_DIR / "state.db"

    @property
    def instructions_dir(self) -> pathlib.Path:
        return INSTRUCTIONS_DIR


def _merge(dc: Any, raw: dict) -> Any:
    """Fill a dataclass from a dict, ignoring unknown keys (so an old config keeps working)."""
    fields = {f.name: f for f in dataclasses.fields(dc)}
    for k, v in raw.items():
        if k not in fields:
            continue
        cur = getattr(dc, k)
        if dataclasses.is_dataclass(cur) and isinstance(v, dict):
            _merge(cur, v)
        else:
            setattr(dc, k, v)
    return dc


@lru_cache(maxsize=1)
def load() -> Settings:
    s = Settings()
    if CONFIG_PATH.exists():
        raw = tomllib.loads(CONFIG_PATH.read_text())
        checks = raw.pop("checks", [])
        _merge(s, raw)
        s.checks = [_merge(CheckSettings(name="", time="", prompt=""), c) for c in checks]
    for d in (s.data_dir, s.memory_dir, s.transcripts_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
