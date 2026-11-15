"""Configuration: one TOML file for preferences, environment variables (or a .env file) for secrets.

Everything that differs between the cloud deployment and the Mac mini is a URL or a token here, so the
same code runs in both places. `EA_DATA` points at the writable state directory (memory, transcripts,
SQLite); `EA_CONFIG` at the TOML file. Secrets are read from the environment first, then from
`<EA_DATA>/.env` (NAME=value lines), so a Finder- or launchd-started process finds them too.
"""
from __future__ import annotations

import dataclasses
import os
import re
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
        v = re.sub(r"\s+#.*$", "", v)            # `KEY=value   # comment` as in .env.example
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
    provider: str = "anthropic"               # anthropic | openai_compat | agent_sdk (Claude Agent SDK: the Claude Code harness runs the turn)
    model: str = "claude-opus-5-5"
    effort: str = "high"                      # low | medium | high | xhigh | max (Anthropic only)
    max_tokens: int = 16000
    fallbacks: bool = True                    # Anthropic server-side refusal fallbacks (beta)
    thinking_display: str = "summarized"      # summarized: readable reasoning in the app's thinking pane | omitted
    fast_when_chatting: bool = True           # fast mode (premium-priced) on turns where the user is waiting for the reply
                                              # (chat, voice); never on checks, wakeups, heartbeats, ingestion or workers
    # agent_sdk only:
    fast_own_thread: bool = True              # chat (fast) turns on a session of their own, separate from background turns;
                                              # false = one session for everything (every speed switch rewrites the prompt cache)
    fast_keepalive_minutes: int = 55          # re-read the chat thread's prompt cache this long after its last call, so the
                                              # next chat does not pay to rewrite it (the cache lasts 60 min); 0 = off
    cost_alert_usd_per_day: float = 150.0     # charged model spend above this in the last 24 hours wakes the agent to tell the user (0 = off)
    cost_alert_check_hours: float = 1.0       # ...looked at this often (a database query, no model call), and at most one alert per 24 hours
    cost_review_days: int = 7                 # the agent reviews the model costs with the user this often (0 = off)
    fast_keepalive_max_idle_hours: int = 36   # stop the keep-alive this long after the last chat (about where it costs more than one rewrite)
    billing: str = "subscription"             # subscription (the Claude Code login) | api (the ANTHROPIC_API_KEY)
    api_fallback: bool = True                 # when the subscription window is nearly used up, run turns over the API
    api_fallback_utilization: float = 0.9     # ...from this fraction of the window (the CLI reports it), until it resets
    subscription_warn_utilization: float = 0.75   # message the user once per window from this fraction (0 = never)
    openai_base_url: str = "http://127.0.0.1:11434/v1"   # Ollama / vLLM / LM Studio when provider=openai_compat
    openai_api_key_env: str = "OPENAI_COMPAT_API_KEY"
    max_tool_rounds: int = 20
    history_messages: int = 80                # conversation window sent to the model
    max_workers: int = 3                      # background workers the agent may run in parallel (delegate tool)


@dataclasses.dataclass
class SourceSettings:
    """Where the data lives. Either a bridge (cloud deployment, reaching the Mac over Tailscale) or
    direct localhost URLs (running on the Mac itself)."""
    bridge_url: str = ""                      # e.g. http://100.x.y.z:8799  (empty = direct)
    bridge_token_env: str = "EA_BRIDGE_TOKEN"
    tracker_url: str = "http://127.0.0.1:8791"
    tracker_app: str = "~/Documents/agent-coding/project-tracker-and-organizer/dist/Project Tracker.app"  # started by tracker_open when its API is down
    focus_url: str = "http://127.0.0.1:8790"
    bandit_url: str = "http://127.0.0.1:8777"
    exercise_export_url_env: str = "EXERCISE_EXPORT_URL"
    toggl_token_env: str = "TOGGL_API_TOKEN"
    tracker_agent_token_env: str = "AGENT_API_TOKEN"      # the tracker's optional agent-API bearer token
    calendar_ics_urls_env: str = "CALENDAR_ICS_URLS"      # iCal feed URL(s), comma-separated; else via the tracker
    otter_email_env: str = "OTTER_EMAIL"                  # Otter.ai login (read-only transcript access)
    otter_password_env: str = "OTTER_PASSWORD"
    otter_logseq_graph: str = "~/Documents/main-logseq-graph"   # Logseq graph holding the auto-imported Otter pages; "" = none
    otter_sync_minutes: int = 15                          # how often a read may re-pull new recordings from Otter
    otter_ingest: bool = True                             # the scheduler pulls new recordings and the agent reads each one
    otter_ingest_max_chars: int = 60_000                  # transcript text handed to the agent per recording (rest via otter_transcript)
    toggl_workspace_id: int | None = None
    toggl_source: str = "focus_monitor"                   # "focus_monitor": read the Focus Monitor's synced Toggl table (no Toggl API calls,
                                                          # no shared quota; lags Toggl by up to 5 min); "toggl": call Toggl's API directly
    timeout_seconds: float = 20.0
    roam_backup_git: str = ""                             # git URL of the daily Roam backup (JSON); cloned to data/roam/backup and pulled on a schedule
    roam_sync_hours: float = 6.0                          # how often to pull it
    web_browsing: bool = True                             # headless Chromium tools (web_search, web_open, ...); needs `playwright install chromium`


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
    # Dedicated iPhone/Mac apps (apps/Atlas): text notifications over Apple Push Notification service.
    apns_bundle_id: str = "com.elityre.atlas"      # the app's bundle identifier (= APNs topic)
    apns_key_path_env: str = "APNS_KEY_PATH"       # .p8 key from developer.apple.com; relative paths are under data/
    apns_key_id_env: str = "APNS_KEY_ID"
    apns_team_id_env: str = "APNS_TEAM_ID"
    apns_interruption_level: str = "active"        # passive | active | time-sensitive (needs the entitlement) | critical


@dataclasses.dataclass
class DocumentRoot:
    """A folder of Markdown the agent can list and read live, beside instructions/. Read at request time, so the
    agent always sees the current file: nothing is copied or synced."""
    name: str                                 # prefix the agent uses: read_document "<name>/<file>.md"
    path: str                                 # folder on this Mac
    note: str = ""                            # one line telling the agent what the folder is


@dataclasses.dataclass
class CheckSettings:
    name: str
    time: str                                 # "HH:MM" local
    prompt: str
    days: list[str] = dataclasses.field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])


COST_REVIEW_PROMPT = """Model cost review, the one the user asked you to do every {days} days. Read your memory `model-costs` (the
background: fast mode, the two threads, the keep-alive, what each number means and what the options are), then
call model_usage for the last {days} days. Send the user a short check-in with send_message: what was charged to
his usage credits in total and per day, what drove it (chat volume, cold starts, the keep-alive), how it compares
with the roughly $24 a day estimated before the change, and anything that looks wrong or worth changing. This
check-in is always sent: do not end with no_action_needed. Add what you learn to the `model-costs` memory."""

COST_ALERT_PROMPT = """Model cost alert. Model spend charged to the user's usage credits over the last 24 hours is about ${charged:.2f}, above the
${limit:.0f} a day line he asked to be told about. Read your memory `model-costs`, call model_usage, and message
him now with send_message: the figure, what drove it (look at chat volume, cold starts and the keep-alive in the
per-day table), and the options he has (they are in the memory; you cannot change the settings yourself). This
alert is always sent, at most once in 24 hours: do not end with no_action_needed."""

OTTER_RECORDING_PROMPT = """A new Otter recording has arrived and this turn is for reading it. Do what the standing instructions say about
recordings. In short: pull out anything actionable (tasks, deadlines, decisions, things the user asked you to
remember or follow up on) and act on it with the tracker and memory tools; note the gist in memory when it is
worth keeping; message the user only if something is time-sensitive or needs their decision, otherwise
no_action_needed. The transcript is below; otter_transcript gives the rest if it is cut off."""

# Prepended to every proactive turn (scheduled checks, heartbeats, self-set wakeups, new recordings, document
# changes). Asked for by the agent itself on 2026-11-11 so the rule does not depend on its memory.
WAKEUP_RULE = """Before deciding anything on this wake-up, read today's checklist (tracker_checklist) and the running Toggl entry
(toggl_current). Use them to see what the user is doing right now so you do not interrupt, to catch anchors that
are still doable today, and to re-plan the day when it has slipped. Reading more does not mean messaging more."""

HEARTBEAT_PROMPT = """Unscheduled check-in: you woke up on your own, as you do at random intervals of about an hour during the
day. Decide what, if anything, is worth a look right now given the time of day, the recent conversation, pending
reminders and wakeups, and the habit documents: the running Toggl entry and today's focus so far, the tracker's
plan for the rest of the day, an upcoming deadline, something you said you would follow up on. Keep it light:
a few tool calls at most. Message only if there is something genuinely useful to say; most of these should end
with no_action_needed. If something deserves a look at a specific later time, schedule_wakeup for it."""


@dataclasses.dataclass
class HeartbeatSettings:
    """Random, roughly hourly self-initiated turns during the active window, on top of the fixed checks."""
    enabled: bool = False
    mean_minutes: int = 60                    # mean gap between wake-ups (exponential draw, clamped below)
    min_minutes: int = 20
    max_minutes: int = 180
    active_start: str = "08:00"               # local; outside the window the next wake-up waits for the window
    active_end: str = "23:00"
    prompt: str = HEARTBEAT_PROMPT


@dataclasses.dataclass
class Settings:
    agent_name: str = "Atlas"
    user_name: str = "Eli"
    timezone: str = "America/Los_Angeles"
    llm: LLMSettings = dataclasses.field(default_factory=LLMSettings)
    sources: SourceSettings = dataclasses.field(default_factory=SourceSettings)
    channels: ChannelSettings = dataclasses.field(default_factory=ChannelSettings)
    checks: list[CheckSettings] = dataclasses.field(default_factory=list)
    document_roots: list[DocumentRoot] = dataclasses.field(default_factory=list)
    heartbeat: HeartbeatSettings = dataclasses.field(default_factory=HeartbeatSettings)
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
        roots = raw.pop("document_roots", [])
        _merge(s, raw)
        s.checks = [_merge(CheckSettings(name="", time="", prompt=""), c) for c in checks]
        s.document_roots = [_merge(DocumentRoot(name="", path=""), r) for r in roots if r.get("name") and r.get("path")]
    for d in (s.data_dir, s.memory_dir, s.transcripts_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
