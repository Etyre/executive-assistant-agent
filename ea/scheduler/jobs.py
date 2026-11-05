"""Background work: deliver due reminders, and run the configured scheduled checks (habit/metric watches,
morning brief, evening review) as proactive agent turns, once per day each at their time."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from .. import config
from ..agent.core import Agent

log = logging.getLogger(__name__)
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Scheduler:
    def __init__(self, agent: Agent, settings: config.Settings | None = None):
        self.agent = agent
        self.s = settings or config.load()
        self.tz = ZoneInfo(self.s.timezone)

    def due_checks(self, now: dt.datetime) -> list[config.CheckSettings]:
        today = now.date().isoformat()
        out = []
        for c in self.s.checks:
            if DAY_KEYS[now.weekday()] not in [d.lower()[:3] for d in c.days]:
                continue
            hh, mm = (int(x) for x in c.time.split(":"))
            at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now < at or now - at > dt.timedelta(hours=3):          # missed by >3h (machine asleep): skip, don't pile up
                continue
            if self.agent.convo.get(f"check_ran:{c.name}") == today:
                continue
            out.append(c)
        return out

    def tick(self) -> None:
        now = dt.datetime.now(self.tz)
        for r in self.agent.convo.due_reminders():
            log.info("reminder #%s due: %s", r["id"], r["text"])
            self.agent._send_outbound(r["text"])
            self.agent.convo.mark_sent(r["id"])
        for c in self.due_checks(now):
            self.agent.convo.set(f"check_ran:{c.name}", now.date().isoformat())
            log.info("running scheduled check %s", c.name)
            try:
                out = self.agent.run_check(c.name, c.prompt)
                log.info("check %s finished: %s", c.name, (out or "")[:200])
            except Exception:  # noqa: BLE001
                log.exception("check %s failed", c.name)

    async def run(self) -> None:
        log.info("scheduler: %d checks configured", len(self.s.checks))
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.s.reminder_poll_seconds)
