"""The integrations the agent is meant to have, what each one needs to work, and whether it does right now.

One place that both `python -m ea check` and the agent's `integrations_status` tool read, so the agent can keep
its own record of what it has access to, what is still missing, and who has to supply it (see the Integrations
section of STANDING_INSTRUCTIONS.md)."""
from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Callable

from . import config


@dataclasses.dataclass
class Integration:
    key: str
    name: str
    purpose: str
    owner: str                       # who must act to get it working: "Eli" or "Claude"
    needs: list[str]                 # env vars / config keys / files required
    how: str                         # how to obtain what is missing
    probe: Callable[[config.Settings], tuple[str, str]]   # -> (state, detail); state: ok | missing | partial | off
    optional: bool = False


def _env(*names: str) -> list[str]:
    return [n for n in names if not config.secret(n)]


def _llm(s: config.Settings) -> tuple[str, str]:
    if s.llm.provider != "anthropic":
        return "ok", f"local model via {s.llm.provider} ({s.llm.model})"
    return ("ok", f"{s.llm.model}, effort {s.llm.effort}") if config.secret("ANTHROPIC_API_KEY") else ("missing", "ANTHROPIC_API_KEY not set")


def _engine(s: config.Settings) -> tuple[str, str]:
    if s.llm.provider != "agent_sdk":
        return "ok", f"{s.llm.provider} ({s.llm.model})"
    from .agent.conversations import ConversationStore
    from .llm.agent_sdk import AgentSDKProvider, AgentSDKRunner
    r = AgentSDKRunner(s, None, ConversationStore(s.state_db, s.transcripts_dir), lambda **k: None, AgentSDKProvider(s.llm.model, s.llm.effort))
    d = r.describe()
    return ("partial" if d["billing"].startswith("api") else "ok"), f"{d['billing']}; window {d['subscription_window']} {d['subscription_status']} " \
        f"{'' if d['subscription_utilization'] is None else str(int(d['subscription_utilization'] * 100)) + '% '}resets {d['subscription_resets']}"


def _tracker(s: config.Settings) -> tuple[str, str]:
    from .connectors.http import ServiceRouter, SourceError
    from .connectors.project_tracker import ProjectTracker
    try:
        ping = ProjectTracker(ServiceRouter(s), s).ping()
        return "ok", f"reachable; token required: {ping.get('token_required')}"
    except SourceError as e:
        return "missing", f"not reachable ({str(e)[:80]}); the Task Tracker app must be running on the Mac"


def _local(url_attr: str, name: str) -> Callable[[config.Settings], tuple[str, str]]:
    def probe(s: config.Settings) -> tuple[str, str]:
        import httpx
        from .connectors.http import ServiceRouter
        url = getattr(s.sources, url_attr)
        try:
            r = httpx.get(url.rstrip("/") + "/", timeout=3)
            return "ok", f"{name} answering on {url}"
        except httpx.HTTPError as e:
            return "missing", f"{name} not reachable on {url} ({type(e).__name__}); must be running on the Mac"
    return probe


def _toggl(s: config.Settings) -> tuple[str, str]:
    return ("ok", "token set") if config.secret(s.sources.toggl_token_env) else ("missing", "TOGGL_API_TOKEN not set")


def _exercise(s: config.Settings) -> tuple[str, str]:
    return ("ok", "export URL set") if config.secret(s.sources.exercise_export_url_env) else ("missing", "EXERCISE_EXPORT_URL not set")


def _calendar(s: config.Settings) -> tuple[str, str]:
    return ("ok", "iCal feed(s) configured") if config.secret(s.sources.calendar_ics_urls_env) else ("partial", "no CALENDAR_ICS_URLS; events come from the tracker's single-day view only")


def _otter(s: config.Settings) -> tuple[str, str]:
    from .connectors.otter import Otter
    o = Otter(s)
    st = o.status()
    if o.live_enabled():
        return "ok", f"login set; {st['recordings']} recordings indexed ({st['first_date']}..{st['last_date']}); ingestion {'on' if s.sources.otter_ingest else 'off'}"
    return "partial", f"archive only: {st['recordings']} recordings to {st['last_date']}; no live pull or ingestion of new recordings (OTTER_EMAIL/OTTER_PASSWORD not set)"


def _roam(s: config.Settings) -> tuple[str, str]:
    from .connectors.roam import Roam
    r = Roam(s)
    if not r.enabled():
        return "missing", f"no export in {r.folder}"
    st = r.status()
    b = st.get("backup") or {}
    if b.get("git"):
        return "ok", f"{st['pages']} pages / {st['blocks']} blocks from the git backup (commit {b.get('commit_date')}, pulled {b.get('last_pull')}), journals to {st['last_journal']}"
    return "partial", (f"{st['pages']} pages / {st['blocks']} blocks from {st['files']}, journals to {st['last_journal']}; "
                       "refreshes only when a new export is dropped (set sources.roam_backup_git for daily sync)")


def _push(s: config.Settings) -> tuple[str, str]:
    from .channels.push import Push
    p = Push(s)
    n = len(p.devices.all())
    if p.enabled():
        return ("ok" if n else "partial"), f"APNs configured; {n} device(s) registered"
    return "partial", f"Apple push not configured ({p.reason_disabled}); the Mac app still gets notifications over its live connection while it runs; the iPhone app needs this"


def _twilio(s: config.Settings) -> tuple[str, str]:
    ch = s.channels
    missing = _env(ch.twilio_account_sid_env, ch.twilio_auth_token_env)
    if not ch.twilio_from_number:
        missing.append("channels.twilio_from_number")
    if not ch.twilio_allowed_callers:
        missing.append("channels.twilio_allowed_callers (Eli's number)")
    if not ch.public_base_url:
        missing.append("channels.public_base_url")
    return ("ok", f"calls from {ch.twilio_from_number} to {ch.twilio_allowed_callers}") if not missing else ("missing", "needs " + ", ".join(missing))


def _public_url(s: config.Settings) -> tuple[str, str]:
    return ("ok", s.channels.public_base_url) if s.channels.public_base_url else ("missing", "no public HTTPS URL; needed by Twilio and by the iPhone app away from home")


def _documents(s: config.Settings) -> tuple[str, str]:
    if not s.document_roots:
        return "off", "no document roots configured"
    parts, bad = [], []
    for r in s.document_roots:
        folder = pathlib.Path(r.path).expanduser()
        n = len(list(folder.rglob("*.md"))) if folder.exists() else 0
        (parts if folder.exists() else bad).append(f"{r.name}: {n} files" if folder.exists() else f"{r.name}: {folder} missing")
    return ("missing" if bad else "ok"), "; ".join(parts + bad) + " (read live, always current)"


def _browser(s: config.Settings) -> tuple[str, str]:
    if not s.sources.web_browsing:
        return "off", "disabled in config"
    import pathlib as _p
    cache = _p.Path.home() / "Library/Caches/ms-playwright"
    have = any(p.name.startswith("chromium") for p in cache.glob("*")) if cache.exists() else False
    return ("ok", "headless Chromium installed") if have else ("missing", "run `.venv/bin/python -m playwright install chromium`")


def _telegram(s: config.Settings) -> tuple[str, str]:
    return ("ok", "configured") if config.secret(s.channels.telegram_token_env) else ("off", "not used (Eli chose the dedicated apps instead, 2026-11-05)")


INTEGRATIONS: list[Integration] = [
    Integration("llm", "Claude (the model)", "everything", "Eli", ["ANTHROPIC_API_KEY"], "console.anthropic.com -> API keys", _llm),
    Integration("engine", "Engine / billing", "which account each turn runs on", "Eli", ["claude login on this Mac (subscription)", "ANTHROPIC_API_KEY (fallback)"],
                "`claude` login for the subscription; API key in data/.env; llm.provider in config.toml picks", _engine),
    Integration("tracker", "Project Tracker", "tasks, projects, the day's plan (read/write)", "Eli", ["Task Tracker.app running", "AGENT_API_TOKEN (optional)"],
                "launch dist/Task Tracker.app; optional token in its Settings -> Assistant agent, mirrored as AGENT_API_TOKEN", _tracker),
    Integration("focus", "Focus Monitor", "window-level focus/distraction metrics", "Eli", ["Focus Monitor running on :8790"], "launch the Focus Monitor app", _local("focus_url", "Focus Monitor")),
    Integration("bandit", "Sleep Bandit + Oura", "sleep, HRV, readiness, checklist (read-only)", "Eli", ["Sleep Bandit running on :8777"], "launch Sleep Bandit (launchd: com.elityre.sleep-bandit)", _local("bandit_url", "Sleep Bandit")),
    Integration("toggl", "Toggl", "time entries", "Eli", ["TOGGL_API_TOKEN"], "Toggl -> Profile -> API token, into data/.env", _toggl),
    Integration("exercise", "Exercise log", "workout sessions", "Eli", ["EXERCISE_EXPORT_URL"], "the Apps Script /exec URL from the tracker's Settings page, into data/.env", _exercise, optional=True),
    Integration("calendar", "Google Calendar", "events (iCal feeds)", "Eli", ["CALENDAR_ICS_URLS"], "Google Calendar -> settings -> 'Secret address in iCal format', comma-separated into data/.env", _calendar, optional=True),
    Integration("otter", "Otter.ai", "every voice recording, read as it arrives + full-text history", "Eli", ["OTTER_EMAIL", "OTTER_PASSWORD"],
                "set an Otter password on otter.ai (Google-sign-in accounts have none), put both in data/.env, restart serve, run `python -m ea otter --all` once", _otter),
    Integration("roam", "Roam Research", "every page and journal (search, pages, backlinks)", "Eli", ["an export in data/roam/", "daily sync: GitHub backup repo name, format, access method"],
                "drop Export All -> JSON (or the Markdown zip) in data/roam/; for daily updates Eli names the GitHub backup repo and Claude builds the sync", _roam),
    Integration("push", "Apple push (iPhone + Mac apps)", "notifications when the app is closed / phone locked", "Eli", ["Apple Developer Program (paid)", "APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID"],
                "join the Developer Program; developer.apple.com -> Keys -> APNs key (.p8) into data/, ids into data/.env; see apps/Atlas/README.md", _push),
    Integration("public_url", "Public HTTPS URL", "lets Twilio and the iPhone app reach the server from outside", "Eli", ["channels.public_base_url"],
                "install Tailscale, enable Funnel, `tailscale funnel --bg 8800`, put the URL in config.toml", _public_url),
    Integration("twilio", "Twilio (phone calls)", "the agent calls Eli", "Eli", ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "channels.twilio_from_number", "channels.twilio_allowed_callers", "public URL"],
                "twilio.com: buy a number, set its voice webhook to <public_base_url>/twilio/voice, put SID/token in data/.env and the numbers in config.toml", _twilio),
    Integration("documents", "Document roots (elityre.com source docs)", "Eli's written policies, habits, routines, read live from disk", "Eli",
                ["[[document_roots]] entries in config.toml"], "add a [[document_roots]] block with name, path and note; files are read at request time", _documents),
    Integration("browser", "Web browsing", "headless Chromium: search, read pages, follow links, fill forms", "Claude",
                ["playwright + chromium"], "`pip install playwright && python -m playwright install chromium`", _browser),
    Integration("telegram", "Telegram", "legacy chat channel", "Eli", ["TELEGRAM_BOT_TOKEN"], "not planned", _telegram, optional=True),
]


def status(settings: config.Settings | None = None, live: bool = True) -> list[dict[str, Any]]:
    s = settings or config.load()
    out = []
    for i in INTEGRATIONS:
        try:
            state, detail = i.probe(s)
        except Exception as e:  # noqa: BLE001 - a probe must never break the report
            state, detail = "missing", f"probe failed: {type(e).__name__}: {e}"
        out.append({"key": i.key, "name": i.name, "purpose": i.purpose, "state": state, "detail": detail,
                    "needs": i.needs, "how": i.how, "owner": i.owner, "optional": i.optional})
    return out
