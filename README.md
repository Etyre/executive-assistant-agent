# Executive assistant agent

A personal executive-assistant agent: it watches your metrics (Toggl, Focus Monitor, Oura via Sleep
Bandit, exercise log), keeps your Project Tracker current and can set its daily schedule, keeps a dated,
searchable memory, and talks with you on Telegram, through an iPhone Shortcut (one button, dictated), a
web page, and live phone calls. It runs on Claude Opus 5.5 by default and can be moved to a Mac mini
and/or switched to a locally hosted open-weight model without changing anything but config.

```
ea/
  agent/      core.py (the loop), prompt.py (system prompt), conversations.py (history, transcripts, reminders)
  llm/        base.py (neutral shapes), anthropic_provider.py, openai_compat_provider.py, factory.py
  tools/      registry.py + data_tools, tracker_tools, memory_tools, assistant_tools
  connectors/ http.py (router: bridge vs localhost), toggl, focus_monitor, sleep_bandit (+Oura), exercise, project_tracker
  memory/     store.py  (Markdown files + FTS5 index, dated provenance)
  channels/   telegram.py, web.py (Shortcut endpoint, chat page, Twilio webhooks), twilio_voice.py
  scheduler/  jobs.py   (reminders, scheduled proactive checks)
  stt.py, config.py, __main__.py
instructions/ STANDING_INSTRUCTIONS.md, USER_SYSTEM_PROMPT.md, MEMORY_CONVENTIONS.md, habits/*.md
bridge/       mac_bridge.py (+launchd) - lets a cloud agent reach the Mac's local servers over Tailscale
deploy/       Dockerfile, docker-compose.yml, gcp/ (VM scripts + README), macmini/ (launchd + install.sh)
ios/          SHORTCUT.md - Action button / Siri setup
tests/        pytest suite (no network, scripted model)
data/         (created) memory/, transcripts/, state.db, .env   - this folder is the agent's whole state
```

## Quick start on this Mac

```bash
cd executive-assistant-agent
/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt   # done already
cp config.example.toml config.toml           # edit: agent_name, timezone, telegram_allowed_user_ids, checks
cp .env.example data/.env                    # fill in ANTHROPIC_API_KEY, TOGGL_API_TOKEN, EXERCISE_EXPORT_URL, TELEGRAM_BOT_TOKEN, EA_WEB_TOKEN
.venv/bin/python -m ea check                 # what is configured and reachable
.venv/bin/python -m ea chat                  # talk to it in the terminal
.venv/bin/python -m ea serve                 # Telegram + web/Shortcut endpoint (:8800) + scheduler
```

The Project Tracker, Focus Monitor and Sleep Bandit must be running (they are, as login items). The
tracker's agent API (`/api/agent`) and Sleep Bandit's agent routes are uncommitted additions in those repos;
a running copy started before they were added needs a restart (quit and reopen the Task Tracker app;
`launchctl kickstart -k gui/$(id -u)/com.elityre.sleep-bandit`).

## What the agent can see and do

| Source | Access | How |
|---|---|---|
| Toggl Track | read | Toggl API v9 with `TOGGL_API_TOKEN` (current entry, entries, per-day/project summary) |
| Focus Monitor | read | `GET /api/metrics`, `/api/day` on :8790 (daily focus spans, distractions, totals) |
| Oura ring | read | through Sleep Bandit's OAuth token: new `GET /api/oura/collection?name=daily_readiness&days=N` on :8777 (sleep, readiness, activity, stress, heartrate…) |
| Sleep Bandit | read | new `GET /api/agent/summary?days=N` on :8777: nights, metrics, tonight's checklist, verdicts, HRV readiness status (no planning side effect) |
| Exercise log | read | the exercise-tracker Apps Script `?action=export` (`EXERCISE_EXPORT_URL`) |
| Calendar | read | the same iCal feed(s) the tracker uses (`CALENDAR_ICS_URLS`), any date range up to 62 days; falls back to the tracker's single-day `GET /api/calendar` |
| Project Tracker | read + write | through the tracker's **agent API** (`/api/agent/*`, sends `X-Agent-Name` and the optional `AGENT_API_TOKEN`): outline and task search, tasks/projects created with agent attribution, signed notes on any item, quick-add, the day's scheduling context (free windows, prioritised candidates, rules), **set the schedule** block by block (pinned against the tracker's re-planner, with what did not fit returned), steer a day in prose (instructions the tracker re-plans with), rebuild, unpin; plus feed, checklist ticks, complete/update task from the plain API |
| Memory | read + write | `data/memory/*.md` with `sources` (conversation dates + channel) and `edits` (dates) in the frontmatter; FTS5 search; transcripts in `data/transcripts/YYYY-MM-DD.jsonl` |
| Reminders | write | delivered on Telegram at the time |
| You | message | `send_message` during scheduled checks; `end_conversation` per the user-level prompt |

The standing instructions and user-level system prompt are the Markdown files in `instructions/`; edit
them and the next turn uses the new text. Drop one file per habit into `instructions/habits/` (see the
README and example there) and the scheduled checks compare the data against them.

## Channels

- **Telegram** (phone + laptop): make a bot with @BotFather, put the token in `.env`, your numeric user id
  in `channels.telegram_allowed_user_ids` (ask @userinfobot). Text and voice notes (transcribed with
  OpenAI or Deepgram if a key is set, or local faster-whisper). Anyone not on the list is ignored.
- **iPhone one-button / Siri "Atlas …"**: `ios/SHORTCUT.md`. Needs the agent reachable over HTTPS
  (`channels.public_base_url`) and `EA_WEB_TOKEN`.
- **Web chat** for the laptop: `http://<host>:8800/` (paste the token once).
- **Phone calls**: a Twilio number whose Voice webhook is `https://<public_base_url>/twilio/voice`. Twilio
  does speech recognition and the voice; the agent answers in short spoken sentences. Signature-checked,
  caller allow-listed.
- **Scheduled checks** (`[[checks]]` in config.toml): proactive turns at set times. Nothing is sent unless
  the agent decides to; "all on track" ends silently.

## Model

`claude-opus-5-5` with adaptive thinking, `effort = "high"`, prompt caching on the stable system block and
the conversation history, and Anthropic's server-side refusal fallbacks (beta) switched on, which route
a safety-classifier refusal to another Claude model instead of returning an error; set `fallbacks = false`
to turn that off. To swap in an open-weight model served by Ollama / vLLM / LM Studio, set
`provider = "openai_compat"` and `openai_base_url` (see `config.example.toml`). The conversation history
is kept in Anthropic's content-block shape and translated at the provider edge, so a switch mid-life keeps
all memory and history.

## Deploying

- **Google Cloud now**: `deploy/gcp/README.md` (small VM + Docker + Tailscale; the Mac runs the bridge).
- **Mac mini later**: copy the repo and `data/`, set `sources.bridge_url = ""`, run `deploy/macmini/install.sh`.
- Same `deploy/docker-compose.yml` works in both places.

## Security notes

- The local servers have no auth of their own; they are only reachable on localhost, and from the cloud
  only through the bridge (bearer token, Tailscale-only bind, explicit allow-list of the few tracker writes
  the agent needs, side-effecting reads blocked).
- Inbound to the agent: Telegram is pull (no open port); `/api/message` needs the bearer token; Twilio
  webhooks must carry a valid signature and come from an allowed number. With `EA_WEB_TOKEN` unset the
  endpoint refuses everything.
- Secrets live in `data/.env` (mode 600) or the environment, never in config or code. `data/` is gitignored.
- The agent's writes are limited to the Project Tracker and its own memory/reminders; every tool call is logged.

## Development

```bash
.venv/bin/python -m pytest -q        # 25 tests, no network
.venv/bin/python -m ea run-check morning   # run one scheduled check by hand
.venv/bin/python -m ea reindex       # after editing memory files by hand
```

Related changes in sibling projects: `project-tracker-and-organizer` has the agent API in
`tracker/agent_api.py` (see its README, "Agent API"); `sleep-bandit` gained `GET /api/agent/summary`,
`GET /api/oura/collection` and `oura.fetch_collection` for this agent.
