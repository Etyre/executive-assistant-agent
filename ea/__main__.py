"""CLI.

  python -m ea serve        run everything: web (Shortcut, chat page, Twilio), Telegram, scheduler
  python -m ea chat         talk to the agent in the terminal
  python -m ea check        show what is configured and reachable
  python -m ea run-check N  run one scheduled check now (by name)
  python -m ea reindex      rebuild the memory search index from the Markdown files
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from . import config
from .agent.core import Agent
from .channels.telegram import Telegram
from .channels.web import create_app
from .scheduler.jobs import Scheduler


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_serve(args: argparse.Namespace) -> None:
    s = config.load()
    agent = Agent(s)
    tg = Telegram(agent, s)
    sched = Scheduler(agent, s)
    app = create_app(agent, s)

    async def main() -> None:
        server = uvicorn.Server(uvicorn.Config(app, host=s.channels.web_host, port=s.channels.web_port, log_level="info"))
        await asyncio.gather(server.serve(), tg.run(), sched.run())

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
    print(f"telegram: {'token set' if config.secret(s.channels.telegram_token_env) else 'NOT SET'}; allowed users: {s.channels.telegram_allowed_user_ids or 'NONE'}")
    print(f"web: {s.channels.web_host}:{s.channels.web_port}; token {'set' if config.secret(s.channels.web_token_env) else 'NOT SET'}; public_base_url {s.channels.public_base_url or '(none)'}")
    print(f"twilio: {'configured' if config.secret(s.channels.twilio_auth_token_env) and s.channels.twilio_from_number else 'not configured'}")
    from . import stt
    print(f"speech-to-text: {stt.provider_name(s)}")
    print(f"scheduled checks: {[c.name + ' ' + c.time for c in s.checks] or 'none'}")
    if args.llm:
        agent = Agent(s)
        print("llm reply:", agent.handle("Reply with the single word: ready", channel="cli", store_user=False))


def cmd_run_check(args: argparse.Namespace) -> None:
    s = config.load()
    c = next((c for c in s.checks if c.name == args.name), None)
    if not c:
        sys.exit(f"no check named {args.name!r}; have {[c.name for c in s.checks]}")
    agent = Agent(s)
    tg = Telegram(agent, s)
    if tg.enabled():
        agent.outbound = tg.send_sync
    print(agent.run_check(c.name, c.prompt))


def cmd_reindex(args: argparse.Namespace) -> None:
    from .memory.store import MemoryStore
    n = MemoryStore(config.load().memory_dir).reindex()
    print(f"indexed {n} memories")


def main() -> None:
    p = argparse.ArgumentParser(prog="ea", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("chat").set_defaults(fn=cmd_chat)
    c = sub.add_parser("check"); c.add_argument("--llm", action="store_true", help="also make one model call"); c.set_defaults(fn=cmd_check)
    r = sub.add_parser("run-check"); r.add_argument("name"); r.set_defaults(fn=cmd_run_check)
    sub.add_parser("reindex").set_defaults(fn=cmd_reindex)
    args = p.parse_args()
    _setup_logging(args.verbose)
    args.fn(args)


if __name__ == "__main__":
    main()
