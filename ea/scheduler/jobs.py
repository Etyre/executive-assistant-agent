"""Background work, polled every `reminder_poll_seconds`:

- deliver due reminders (text to the user) and run due **wakeups** (prompts the agent scheduled for itself
  with `schedule_wakeup`, run as proactive turns; repeating ones are re-armed by the store);
- run the configured **scheduled checks** once per day each at their time;
- the **heartbeat**: a proactive turn at random intervals (exponential around `mean_minutes`, clamped to
  min/max) inside the active window. The next due time is kept in the conversation KV store, so a restart
  neither resets the clock nor fires immediately; a wake-up missed because the machine slept runs once when
  it is back, then the next interval is drawn.

Nothing in a proactive turn reaches the user unless the model calls send_message."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import time
from typing import Callable
from zoneinfo import ZoneInfo

from .. import config
from ..agent.core import Agent

log = logging.getLogger(__name__)
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HEARTBEAT_KEY = "heartbeat_next_ts"


def _hhmm(s: str) -> dt.time:
    hh, mm = (int(x) for x in s.split(":"))
    return dt.time(hh, mm)


class Scheduler:
    def __init__(self, agent: Agent, settings: config.Settings | None = None, rng: random.Random | None = None,
                 now_fn: Callable[[], dt.datetime] | None = None):
        self.agent = agent
        self.s = settings or config.load()
        self.tz = ZoneInfo(self.s.timezone)
        self.rng = rng or random.Random()
        self._now_fn = now_fn

    def now(self) -> dt.datetime:
        return self._now_fn() if self._now_fn else dt.datetime.now(self.tz)

    # ------------------------------------------------------------------ fixed checks
    def due_checks(self, now: dt.datetime) -> list[config.CheckSettings]:
        today = now.date().isoformat()
        out = []
        for c in self.s.checks:
            if DAY_KEYS[now.weekday()] not in [d.lower()[:3] for d in c.days]:
                continue
            at = now.replace(hour=_hhmm(c.time).hour, minute=_hhmm(c.time).minute, second=0, microsecond=0)
            if now < at or now - at > dt.timedelta(hours=3):          # missed by >3h (machine asleep): skip, don't pile up
                continue
            if self.agent.convo.get(f"check_ran:{c.name}") == today:
                continue
            out.append(c)
        return out

    # ------------------------------------------------------------------ heartbeat
    def in_active_window(self, now: dt.datetime) -> bool:
        start, end = _hhmm(self.s.heartbeat.active_start), _hhmm(self.s.heartbeat.active_end)
        t = now.time().replace(second=0, microsecond=0)
        return start <= t < end if start <= end else (t >= start or t < end)

    def _window_start_after(self, now: dt.datetime) -> dt.datetime:
        start = _hhmm(self.s.heartbeat.active_start)
        cand = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        return cand if cand > now else cand + dt.timedelta(days=1)

    def draw_gap(self) -> dt.timedelta:
        hb = self.s.heartbeat
        minutes = self.rng.expovariate(1.0 / max(1, hb.mean_minutes))
        return dt.timedelta(minutes=min(max(minutes, hb.min_minutes), hb.max_minutes))

    def schedule_next_heartbeat(self, now: dt.datetime) -> dt.datetime:
        """From `now` (or from the next window start if `now` is outside it) plus a random gap, kept inside the window."""
        base = now if self.in_active_window(now) else self._window_start_after(now)
        nxt = base + self.draw_gap()
        if not self.in_active_window(nxt):                             # the gap crossed the end of the window
            nxt = self._window_start_after(nxt) + dt.timedelta(minutes=min(self.s.heartbeat.min_minutes, 30)) * self.rng.random()
        self.agent.convo.set(HEARTBEAT_KEY, nxt.timestamp())
        return nxt

    def next_heartbeat(self) -> dt.datetime | None:
        ts = self.agent.convo.get(HEARTBEAT_KEY)
        return dt.datetime.fromtimestamp(ts, self.tz) if ts else None

    def heartbeat_due(self, now: dt.datetime) -> bool:
        hb = self.s.heartbeat
        if not hb.enabled:
            return False
        nxt = self.next_heartbeat()
        if nxt is None:
            nxt = self.schedule_next_heartbeat(now)
            log.info("heartbeat: first wake-up at %s", nxt.strftime("%a %H:%M"))
            return False
        if now < nxt:
            return False
        if not self.in_active_window(now):                              # due time has passed but we are outside the window
            self.schedule_next_heartbeat(now)
            return False
        return True

    # ------------------------------------------------------------------ otter ingestion
    def ingest_otter(self, now: dt.datetime | None = None) -> list[str]:
        """Every `otter_sync_minutes`: pull new recordings; the agent reads each unseen one in its own turn.
        Returns the otids handed to the agent."""
        src = self.s.sources
        otter = getattr(self.agent, "otter", None)
        if not src.otter_ingest or otter is None or not otter.live_enabled():
            return []
        now = now or self.now()
        last = self.agent.convo.get("otter_ingest_last_ts") or 0.0
        if now.timestamp() - last < src.otter_sync_minutes * 60:
            return []
        self.agent.convo.set("otter_ingest_last_ts", now.timestamp())
        if not self.agent.convo.get("otter_ingest_started"):            # first run: history is not news
            n = otter.mark_all_seen()
            self.agent.convo.set("otter_ingest_started", now.isoformat())
            log.info("otter ingestion starting; %d existing recordings marked as already seen", n)
        try:
            r = otter.sync()
            log.info("otter sync: %s", r)
        except Exception as e:  # noqa: BLE001 - keep what we have; try again next time
            log.warning("otter sync failed: %s", e)
        done = []
        for rec in otter.unseen(limit=5):
            otter.mark_seen(rec["otid"])                                  # first, so a crash mid-turn cannot loop
            full = otter.get(rec["otid"], max_chars=src.otter_ingest_max_chars) or {}
            header = f"new Otter recording: {rec.get('title') or 'untitled'} ({rec['date']} {rec.get('start_time') or ''}, {rec.get('minutes', '?')} min, otid {rec['otid']})"
            body = (f"{config.OTTER_RECORDING_PROMPT}\n\n--- transcript ---\n{full.get('text', '')}"
                    + ("\n[transcript continues; use otter_transcript with offset %s]" % full["next_offset"] if full.get("truncated") else ""))
            log.info("reading %s", header)
            try:
                out = self.agent.run_check(header, body, kind="otter recording")
                log.info("recording %s read: %s", rec["otid"], (out or "")[:200])
            except Exception:  # noqa: BLE001
                log.exception("reading recording %s failed", rec["otid"])
            done.append(rec["otid"])
        return done

    # ------------------------------------------------------------------ document changes
    def watch_documents(self, now: dt.datetime | None = None, settle_seconds: float = 120.0, max_diff: int = 12_000) -> list[str]:
        """Documents in the configured roots that changed since the agent last saw them: once a file has been still
        for `settle_seconds` (the user may still be typing), the agent gets a turn with the diff. The first pass only
        takes snapshots. Returns the names handed to the agent."""
        import difflib
        import pathlib
        import time
        roots = getattr(self.s, "document_roots", [])
        if not roots:
            return []
        convo = self.agent.convo
        first = not convo.get("docs_watch_started")
        done = []
        for r in roots:
            folder = pathlib.Path(r.path).expanduser()
            if not folder.exists():
                continue
            for p in sorted(folder.rglob("*.md")):
                if not p.stem or p.name.startswith("."):
                    continue
                name = f"{r.name}/{p.relative_to(folder)}"
                try:
                    st = p.stat()
                except OSError:
                    continue
                snap = convo.doc_snapshot(name)
                if snap and abs(snap[0] - st.st_mtime) < 1e-6:
                    continue
                if first or snap is None:
                    convo.set_doc_snapshot(name, st.st_mtime, p.read_text(errors="replace"))   # new file: just remember it
                    if not first and snap is None:
                        log.info("new document %s noted", name)
                    continue
                if time.time() - st.st_mtime < settle_seconds:
                    continue                                                        # still being edited
                new_text = p.read_text(errors="replace")
                convo.set_doc_snapshot(name, st.st_mtime, new_text)                 # first, so a crash cannot loop
                if new_text == snap[1]:
                    continue
                diff = "".join(difflib.unified_diff(snap[1].splitlines(True), new_text.splitlines(True), fromfile=f"{name} (before)", tofile=f"{name} (after)", n=2))
                if len(diff) > max_diff:
                    diff = diff[:max_diff] + f"\n… [{len(diff) - max_diff} more chars; read_document {name} for the whole page]"
                header = f"document changed: {name}"
                body = (f"The user edited {name} ({r.note or 'a shared document'}). The diff is below. If the change touches a policy, "
                        "habit, priority or fact you rely on, update your memory (and anything you had planned on the old "
                        "version); treat it as the user's considered statement. Message the user only if the change needs a "
                        "decision or conflicts with something you are doing for them; otherwise no_action_needed.\n\n" + diff)
                log.info("reading changed document %s (%d chars of diff)", name, len(diff))
                try:
                    out = self.agent.run_check(header, body, kind="document change")
                    log.info("document change %s handled: %s", name, (out or "")[:200])
                except Exception:  # noqa: BLE001
                    log.exception("document change %s failed", name)
                done.append(name)
        if first:
            convo.set("docs_watch_started", (now or self.now()).isoformat())
            log.info("document watch started; snapshots taken")
        return done

    # ------------------------------------------------------------------ one pass
    def tick(self) -> None:
        now = self.now()
        try:
            self.ingest_otter(now)
        except Exception:  # noqa: BLE001
            log.exception("otter ingestion failed")
        try:
            self.watch_documents(now)
        except Exception:  # noqa: BLE001
            log.exception("document watch failed")
        roam = getattr(self.agent, "roam", None)
        if roam is not None and self.s.sources.roam_backup_git:
            try:
                roam.sync_backup()                      # clones once, then pulls every roam_sync_hours
            except Exception:  # noqa: BLE001
                log.exception("roam backup sync failed")
        for r in self.agent.convo.due_reminders(now.timestamp()):
            self.agent.convo.mark_sent(r["id"], now.timestamp())       # first, so a crash mid-turn cannot loop
            if (r.get("kind") or "message") == "wakeup":
                due = dt.datetime.fromtimestamp(r["due_ts"], self.tz)
                late = now - due
                header = f"wakeup #{r['id']}" + (f", set for {due:%H:%M}, running {int(late.total_seconds() // 60)} min late" if late > dt.timedelta(minutes=10) else "")
                if r.get("every_minutes"):
                    header += f", repeats every {r['every_minutes']} min (cancel_reminder {r['id']} to stop)"
                log.info("running %s: %s", header, r["text"][:120])
                try:
                    out = self.agent.run_check(header, r["text"], kind="wakeup")
                    log.info("wakeup #%s finished: %s", r["id"], (out or "")[:200])
                except Exception:  # noqa: BLE001
                    log.exception("wakeup #%s failed", r["id"])
            else:
                log.info("reminder #%s due: %s", r["id"], r["text"])
                self.agent._send_outbound(r["text"])
        for c in self.due_checks(now):
            self.agent.convo.set(f"check_ran:{c.name}", now.date().isoformat())
            log.info("running scheduled check %s", c.name)
            try:
                out = self.agent.run_check(c.name, c.prompt)
                log.info("check %s finished: %s", c.name, (out or "")[:200])
            except Exception:  # noqa: BLE001
                log.exception("check %s failed", c.name)
        if self.heartbeat_due(now):
            nxt = self.schedule_next_heartbeat(now)                     # before the turn: a crash must not re-fire
            log.info("running heartbeat; next at %s", nxt.strftime("%a %H:%M"))
            try:
                out = self.agent.run_check(f"{now:%H:%M}, next around {nxt:%H:%M}", self.s.heartbeat.prompt, kind="heartbeat")
                log.info("heartbeat finished: %s", (out or "")[:200])
            except Exception:  # noqa: BLE001
                log.exception("heartbeat failed")

    async def _keepalive(self) -> None:
        """Its own loop, not part of tick(): a tick can sit in a background turn for minutes, and the chat
        session's cache has to be re-read in the last few minutes before it expires."""
        while True:
            try:
                await asyncio.to_thread(self.agent.keep_fast_warm)
            except Exception:  # noqa: BLE001
                log.exception("fast cache keep-alive failed")
            await asyncio.sleep(min(30, self.s.reminder_poll_seconds))

    async def run(self) -> None:
        hb = self.s.heartbeat
        log.info("scheduler: %d checks configured; heartbeat %s", len(self.s.checks),
                 f"on (mean {hb.mean_minutes} min, {hb.active_start}-{hb.active_end})" if hb.enabled else "off")
        asyncio.get_running_loop().create_task(self._keepalive())
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.s.reminder_poll_seconds)
