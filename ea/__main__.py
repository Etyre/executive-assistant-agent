"""Executive assistant agent (Atlas): a long-lived Claude agent over Eli's own data (Toggl, Focus Monitor, Oura via
Sleep Bandit, exercise log, calendar, Otter transcripts, Roam graph, Project Tracker read/write) that talks through
the Atlas iPhone/Mac apps, a web chat, an iPhone Shortcut, Telegram and phone calls, and acts on its own on a
schedule. `python -m ea serve` is what launchd runs (com.elityre.executive-assistant).

Where things live:
  config      config.toml in the repo (EA_CONFIG overrides), secrets in data/.env (or the environment)
  state       data/  (memory/, transcripts/, state.db, otter.db, roam.db, workspace/, images/; EA_DATA overrides)
  logs        ~/Library/Logs/executive-assistant.log under launchd (stdout+stderr, timestamped); stdout when run by hand
  LLM inbox   ~/Library/Application Support/LLMInbox/ (shared across projects; `llm-inbox` reads it)

Commands:
  python -m ea serve        run everything: web (apps, Shortcut, chat page, Twilio), push, Telegram, scheduler
  python -m ea chat         talk to the agent in the terminal
  python -m ea check        show what is configured and reachable
  python -m ea run-check N  run one scheduled check now (by name)
  python -m ea reindex      rebuild the memory search index from the Markdown files
  python -m ea push-test    send a test notification to the registered iPhone/Mac apps (Apple push)
  python -m ea notify TEXT  send TEXT to the user through the running server, as the agent would
  python -m ea roam         index the Roam export dropped in data/roam/
  python -m ea run TEXT     give the running agent a task to do on its own (not a chat message)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from . import config
from .agent.core import Agent
from .channels.events import BUS, PRESENCE
from .channels.push import Push
from .channels.telegram import Telegram
from .channels.web import create_app
from .scheduler.jobs import Scheduler


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _outbound(agent: Agent, push: Push, tg: Telegram | None = None):
    """Everything the agent sends on its own initiative goes to: the apps (push), every open app/web page (live
    event), and Telegram if that legacy channel is configured."""
    import datetime as dt

    def send(text: str) -> None:
        quiet = PRESENCE.in_chat()          # user is in the chat somewhere: show it there, don't interrupt
        BUS.publish({"type": "message", "who": "it", "text": text, "channel": "proactive", "quiet": quiet,
                     "ts": dt.datetime.now().isoformat(timespec="seconds")})
        if not quiet:
            push.send_sync(text)
        if tg is not None and tg.enabled():
            tg.send_sync(text)
    return send


def cmd_serve(args: argparse.Namespace) -> None:
    s = config.load()
    agent = Agent(s)
    push = Push(s)
    tg = Telegram(agent, s)
    sched = Scheduler(agent, s)
    app = create_app(agent, s, push=push, bus=BUS)
    agent.outbound = _outbound(agent, push, tg)
    agent.activity = lambda e: BUS.publish({"type": "activity", **e})
    agent.on_message = lambda e: BUS.publish({**e, "quiet": True if e.get("who") == "me" else PRESENCE.in_chat()})
    agent.ui = lambda e: BUS.publish({"type": "ui", **e})
    agent.workers.on_change = lambda running: BUS.publish({"type": "workers", "running": running})
    logging.getLogger(__name__).info("push: %s", push.describe())

    async def main() -> None:
        # timeout_graceful_shutdown: without it uvicorn waits forever for open event streams, so a restart leaves the
        # old process alive (still pinging the apps from a dead bus) next to the new one (seen 2026-11-06).
        server = uvicorn.Server(uvicorn.Config(app, host=s.channels.web_host, port=s.channels.web_port, log_level="info",
                                               timeout_graceful_shutdown=3))
        tasks = [server.serve(), sched.run()]
        if tg.enabled():
            tasks.append(tg.run())
        await asyncio.gather(*tasks)

    asyncio.run(main())


def cmd_chat(args: argparse.Namespace) -> None:
    agent = Agent()
    print(f"{agent.settings.agent_name} ({agent.provider.name} / {agent.settings.llm.model}). Ctrl-D to quit.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except EOFError:
            print()
            return
        if not text:
            continue
        print(f"\n{agent.settings.agent_name}> {agent.handle(text, channel='cli')}")


def cmd_check(args: argparse.Namespace) -> None:
    s = config.load()
    print(f"agent: {s.agent_name}   data: {s.data_dir}   config: {config.CONFIG_PATH} ({'found' if config.CONFIG_PATH.exists() else 'MISSING, defaults in use'})")
    print(f"llm: {s.llm.provider} / {s.llm.model} / effort {s.llm.effort}   anthropic key: {'set' if config.secret('ANTHROPIC_API_KEY') else 'NOT SET (SDK may still find an `ant auth login` profile)'}")
    if s.llm.provider == "agent_sdk":
        from .agent.conversations import ConversationStore
        from .llm.agent_sdk import AgentSDKRunner, AgentSDKProvider
        r = AgentSDKRunner(s, None, ConversationStore(s.state_db, s.transcripts_dir), lambda **k: None, AgentSDKProvider(s.llm.model, s.llm.effort))
        print(f"engine: Claude Agent SDK, billing {s.llm.billing}; API fallback {'on' if s.llm.api_fallback else 'off'} at {int(s.llm.api_fallback_utilization * 100)}%: {r.describe()}")
    from .connectors.exercise import Exercise
    from .connectors.focus_monitor import FocusMonitor
    from .connectors.http import ServiceRouter, SourceError
    from .connectors.project_tracker import ProjectTracker
    from .connectors.sleep_bandit import SleepBandit
    from .connectors.toggl import Toggl
    router = ServiceRouter(s)
    print(f"sources: {router.describe()}")

    def probe(name, fn):
        try:
            out = fn()
            print(f"  ok   {name}: {out}")
        except SourceError as e:
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {name}: {type(e).__name__}: {e}")

    def _tracker():
        t = ProjectTracker(router, s)
        ping = t.ping()
        return f"agent API ok as '{ping.get('agent')}' (token required: {ping.get('token_required')}); {len(t.tree().get('projects', []))} projects"
    probe("project tracker", _tracker)
    from .connectors.calendar import Calendar
    cal = Calendar(s, tracker=ProjectTracker(router, s))
    probe("calendar", lambda: f"{len(cal.events(days=7))} events in the next 7 days via {'ICS feeds' if cal.urls else 'the tracker'}")
    probe("focus monitor", lambda: f"{len(FocusMonitor(router).metrics(3))} days of metrics")
    probe("sleep bandit", lambda: f"{len(SleepBandit(router).summary(3).get('nights', []))} nights")
    probe("oura (via sleep bandit)", lambda: f"{len(SleepBandit(router).oura('daily_readiness', 3).get('data', []))} readiness days")
    tg = Toggl(s)
    probe("toggl", lambda: f"{len(tg.entries(3))} entries in 3 days" if tg.enabled() else "disabled (TOGGL_API_TOKEN not set)")
    ex = Exercise(s)
    probe("exercise log", lambda: f"{len(ex.sessions(30))} sessions in 30 days" if ex.enabled() else "disabled (EXERCISE_EXPORT_URL not set)")
    from .connectors.otter import Otter

    def _otter():
        ot = Otter(s)
        if not ot.enabled():
            return "disabled (no OTTER_EMAIL/OTTER_PASSWORD and no Logseq archive)"
        ot.reindex_archive()
        note = ""
        if ot.live_enabled():
            try:
                r = ot.sync(max_recordings=10)
                note = f"; Otter login ok, {r['added']} new of {r['listed']} newest"
            except SourceError as e:
                note = f"; Otter sync FAILED: {e}"
        else:
            note = "; Otter login not configured (archive only)"
        st = ot.status()
        return f"{st['recordings']} transcripts indexed ({st['first_date']} .. {st['last_date']}){note}"
    probe("otter transcripts", _otter)
    from .connectors.roam import Roam

    def _roam():
        rm = Roam(s)
        if not rm.enabled():
            return f"no export in {rm.folder} (drop Roam's JSON or Markdown export there)"
        st = rm.reindex()
        return f"{st['pages']} pages, {st['blocks']} blocks; journals {st['first_journal']} .. {st['last_journal']}; files {st['files']}"
    probe("roam graph", _roam)
    print(f"push (iPhone/Mac apps): {Push(s).describe()}")
    print(f"telegram (optional): {'token set' if config.secret(s.channels.telegram_token_env) else 'not set'}; allowed users: {s.channels.telegram_allowed_user_ids or 'NONE'}")
    print(f"web: {s.channels.web_host}:{s.channels.web_port}; token {'set' if config.secret(s.channels.web_token_env) else 'NOT SET'}; public_base_url {s.channels.public_base_url or '(none)'}")
    print(f"twilio: {'configured' if config.secret(s.channels.twilio_auth_token_env) and s.channels.twilio_from_number else 'not configured'}")
    from . import stt
    print(f"speech-to-text: {stt.provider_name(s)}")
    print(f"scheduled checks: {[c.name + ' ' + c.time for c in s.checks] or 'none'}")
    hb = s.heartbeat
    from .agent.conversations import ConversationStore
    store = ConversationStore(s.state_db, s.transcripts_dir)
    print(f"heartbeat: {'ON' if hb.enabled else 'off'}; mean {hb.mean_minutes} min (clamped {hb.min_minutes}-{hb.max_minutes}), "
          f"active {hb.active_start}-{hb.active_end}; pending wakeups: {len(store.pending_reminders('wakeup'))}, "
          f"pending reminders: {len(store.pending_reminders('message'))}")
    if args.llm:
        agent = Agent(s)
        print("llm reply:", agent.handle("Reply with the single word: ready", channel="cli", store_user=False))


def cmd_run_check(args: argparse.Namespace) -> None:
    s = config.load()
    agent = Agent(s)
    agent.outbound = _outbound(agent, Push(s), Telegram(agent, s))
    if args.name == "heartbeat":
        print(agent.run_check("manual", s.heartbeat.prompt, kind="heartbeat"))
        return
    c = next((c for c in s.checks if c.name == args.name), None)
    if not c:
        sys.exit(f"no check named {args.name!r}; have {[c.name for c in s.checks] + ['heartbeat']}")
    print(agent.run_check(c.name, c.prompt))


def cmd_push_test(args: argparse.Namespace) -> None:
    s = config.load()
    push = Push(s)
    print(push.describe())
    if not push.enabled():
        sys.exit("push is not configured; set APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID in data/.env")
    for r in push.send(args.text or f"Test notification from {s.agent_name}.", kind="test"):
        print(f"  {r['platform']:5} {r['name'] or r['token'][:12]:24} -> {r['status']} {r['reason']}")


def cmd_usage(args: argparse.Namespace) -> None:
    """Subscription usage as the Claude Code CLI has reported it to the agent (one reading per change)."""
    from .agent.conversations import ConversationStore
    from .llm.agent_sdk import AgentSDKProvider, AgentSDKRunner
    s = config.load()
    r = AgentSDKRunner(s, None, ConversationStore(s.state_db, s.transcripts_dir), lambda **k: None, AgentSDKProvider(s.llm.model, s.llm.effort))
    print(r.prompt_line())
    for x in r.readings(args.limit):
        pct = "" if x["utilization"] is None else f"{x['utilization'] * 100:5.0f}%"
        print(f"  {x['at']}  {x['window'] or '-':14} {x['status']:16} {pct:6}  resets {x['resets']}")


def cmd_roam(args: argparse.Namespace) -> None:
    """Index (or re-index) the Roam export in data/roam/ by hand; the agent also does this lazily."""
    from .connectors.roam import Roam
    rm = Roam(config.load())
    if not rm.enabled():
        sys.exit(f"no Roam export in {rm.folder}: drop the JSON export (Export All -> JSON) or the Markdown export (.zip / .md) there")
    if config.load().sources.roam_backup_git:
        print("backup:", rm.sync_backup(force=True))
    print(rm.reindex(force=args.reindex))


def cmd_run(args: argparse.Namespace) -> None:
    """Give the running agent a task (a proactive turn through the server, so the thinking pane shows it)."""
    import httpx
    s = config.load()
    tok = config.secret(s.channels.web_token_env)
    host = "127.0.0.1" if s.channels.web_host in ("0.0.0.0", "") else s.channels.web_host
    try:
        r = httpx.post(f"http://{host}:{s.channels.web_port}/api/run", json={"text": args.text, "name": args.name},
                       headers={"Authorization": f"Bearer {tok}"}, timeout=600)
    except httpx.HTTPError as e:
        sys.exit(f"server not reachable ({e}); is `python -m ea serve` running?")
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text[:200]}")
    print(r.json()["result"])


def cmd_notify(args: argparse.Namespace) -> None:
    """Through the running server (so open apps and the live feed see it), not a second process."""
    import httpx
    s = config.load()
    tok = config.secret(s.channels.web_token_env)
    if not tok:
        sys.exit(f"{s.channels.web_token_env} is not set")
    host = "127.0.0.1" if s.channels.web_host in ("0.0.0.0", "") else s.channels.web_host
    try:
        r = httpx.post(f"http://{host}:{s.channels.web_port}/api/notify", json={"text": args.text},
                       headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    except httpx.HTTPError as e:
        sys.exit(f"server not reachable on {host}:{s.channels.web_port} ({e}); is `python -m ea serve` running?")
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    print(f"sent; {d['listeners']} live app(s)/page(s) got it, {d['devices']} device(s) registered for Apple push"
          + ("; QUIET (you are in the chat, so no notification)" if d.get("quiet") else "; notified"))


def cmd_reindex(args: argparse.Namespace) -> None:
    from .memory.store import MemoryStore
    n = MemoryStore(config.load().memory_dir).reindex()
    print(f"indexed {n} memories")


def cmd_otter(args: argparse.Namespace) -> None:
    """Build or refresh the Otter transcript index by hand (the agent also does this lazily)."""
    from .connectors.otter import Otter
    ot = Otter(config.load())
    n = ot.reindex_archive(force=args.reindex)
    print(f"logseq archive: {n} recording(s) {'re' if args.reindex else ''}indexed")
    if args.sync or args.all:
        r = ot.sync(max_recordings=args.max, all_history=args.all)
        print(f"otter sync: {r}")
    print(ot.status())


def main() -> None:
    p = argparse.ArgumentParser(prog="ea", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("chat").set_defaults(fn=cmd_chat)
    c = sub.add_parser("check"); c.add_argument("--llm", action="store_true", help="also make one model call"); c.set_defaults(fn=cmd_check)
    r = sub.add_parser("run-check"); r.add_argument("name"); r.set_defaults(fn=cmd_run_check)
    sub.add_parser("reindex").set_defaults(fn=cmd_reindex)
    us = sub.add_parser("usage", help="subscription usage readings reported by the Claude Code CLI"); us.add_argument("--limit", type=int, default=30); us.set_defaults(fn=cmd_usage)
    rm = sub.add_parser("roam", help="index the Roam export in data/roam/"); rm.add_argument("--reindex", action="store_true"); rm.set_defaults(fn=cmd_roam)
    rn = sub.add_parser("run", help="give the running agent a task in the background"); rn.add_argument("text"); rn.add_argument("--name", default="request"); rn.set_defaults(fn=cmd_run)
    nt = sub.add_parser("notify"); nt.add_argument("text"); nt.set_defaults(fn=cmd_notify)
    pt = sub.add_parser("push-test"); pt.add_argument("text", nargs="?", default=""); pt.set_defaults(fn=cmd_push_test)
    o = sub.add_parser("otter", help="index Otter transcripts: the Logseq archive, plus new recordings from Otter with --sync / --all")
    o.add_argument("--sync", action="store_true", help="pull the newest recordings from Otter (needs OTTER_EMAIL/OTTER_PASSWORD)")
    o.add_argument("--all", action="store_true", help="page through Otter's whole history (first import)")
    o.add_argument("--max", type=int, default=40, help="recordings to look at with --sync")
    o.add_argument("--reindex", action="store_true", help="re-parse the Logseq pages even if unchanged")
    o.set_defaults(fn=cmd_otter)
    args = p.parse_args()
    _setup_logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
