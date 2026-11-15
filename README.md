# Executive assistant agent

A personal executive-assistant agent ("Atlas"), built from 2026-11-04: it watches your metrics (Toggl, Focus
Monitor, Oura via Sleep Bandit, exercise log), keeps your Project Tracker current and can set its daily schedule,
keeps a dated, searchable memory, reads your Otter.ai transcripts and Roam graph, and talks with you through its
own iPhone and Mac apps (push notifications), an iPhone Shortcut (one button, dictated), a web page, and live
phone calls. It runs on Claude Opus 5.5 by default and can be moved to a Mac mini and/or switched to a locally
hosted open-weight model without changing anything but config. It was built so that one assistant sees all of
Eli's data sources at once and can act on them on its own schedule, instead of each tracker being a silo.

## How it works

`python -m ea serve` (what launchd runs as `com.elityre.executive-assistant`) starts one process: a FastAPI server
on :8800 (the apps, the Shortcut, the web chat, Twilio webhooks, a live event stream), the push sender, the
scheduler (checks, reminders, wakeups, heartbeat, Otter ingestion, Roam backup sync) and, if configured,
Telegram. Every channel feeds one conversation thread; the agent loop (`ea/agent/core.py`) takes a user turn,
calls tools until the model stops, and returns one reply. Tools are plain Python functions with JSON schemas
(`ea/tools/`), results come back as text, and errors go back to the model as text so one failing source never
ends a turn. The prompt (`ea/agent/prompt.py`) is a cached stable block (identity, the standing instructions and
user-level prompt from `instructions/`, memory conventions) plus a small volatile block rebuilt every call (clock,
channel, memory index, pending reminders). All state is in `data/`.

### Every model call follows the shared LLM standard

Every system prompt starts, word for word, with `~/Documents/agent-coding/shared/llm-system-prompt-preamble.md`
(`ea/contact.py` wraps `prompt.stable_block()` in `llm_contact.system_prompt()`); the `contact_eli` tool is in
the registry like any other tool, so both engines offer it; and after every response (API loop in `core.py`,
every assistant message in `llm/agent_sdk.py`) `contact.check()` looks for a `contact_eli` call or an `@ELI:`
line. On a hit it records the full context in `~/Library/Application Support/LLMInbox/messages.jsonl`, notifies
Eli (Mac notification, ntfy, email) and the turn stops: no further model call, the pending `tool_use` blocks get
a result so the stored history stays valid, and the reply is `(I asked to talk to Eli; recorded in the LLM
inbox as <id>…)`. Eli answers with `llm-inbox show/debrief <id>`. Proactive turns stop the same way; the
notification is what reaches Eli.

### What the agent can see and do

| Source | Access | How |
|---|---|---|
| Toggl Track | read | by default the Focus Monitor's synced copy (`/api/toggl/current`, `/api/toggl/entries`; it polls Toggl every 5 min, so "current" can lag that much); `sources.toggl_source = "toggl"` calls Toggl API v9 directly with `TOGGL_API_TOKEN`. Switched 2026-11-11: the agent's own polling kept hitting Toggl's hourly quota, shared with the Focus Monitor |
| Focus Monitor | read | `GET /api/metrics`, `/api/day` on :8790 (daily focus spans, distractions, totals) |
| Oura ring | read | through Sleep Bandit's OAuth token: new `GET /api/oura/collection?name=daily_readiness&days=N` on :8777 (sleep, readiness, activity, stress, heartrate…) |
| Sleep Bandit | read | new `GET /api/agent/summary?days=N` on :8777: nights, metrics, tonight's checklist, verdicts, HRV readiness status (no planning side effect) |
| Exercise log | read | the exercise-tracker Apps Script `?action=export` (`EXERCISE_EXPORT_URL`) |
| Otter.ai transcripts | read | new recordings through Otter's unofficial API (`OTTER_EMAIL`/`OTTER_PASSWORD`, no third party), plus the 2024+ archive the Roam extension wrote into the Logseq graph (`sources.otter_logseq_graph`); both merged into `data/otter.db` with FTS5 search (`otter_recent`, `otter_search`, `otter_transcript`); `python -m ea otter --all` for the first full pull |
| Calendar | read | the same iCal feed(s) the tracker uses (`CALENDAR_ICS_URLS`), any date range up to 62 days; falls back to the tracker's single-day `GET /api/calendar` |
| Project Tracker | read + write | through the tracker's **agent API** (`/api/agent/*`, sends `X-Agent-Name` and the optional `AGENT_API_TOKEN`): outline and task search, tasks/projects created with agent attribution, signed notes on any item, quick-add, the day's scheduling context (free windows, prioritised candidates, rules), **set the schedule** block by block (pinned against the tracker's re-planner, with what did not fit returned), steer a day in prose (instructions the tracker re-plans with), rebuild, unpin; plus feed, complete/update task from the plain API. The daily checklist is read-only and comes back compact (per row: today, last met, consecutive misses, colour from its steps-to-yellow rule; `days` adds recent raw values, `full` the whole sheet), because the full sheet is ~50 KB, more than the SDK passes back as a tool result. `tracker_open` starts the Project Tracker app (`sources.tracker_app`, opened with `open -g` so it does not take focus) when its API is down; every tracker tool says so when the app is not answering. Only on the Mac itself, not through the bridge |
| Memory | read + write | `data/memory/*.md` with `sources` (conversation dates + channel) and `edits` (dates) in the frontmatter; FTS5 search; transcripts in `data/transcripts/YYYY-MM-DD.jsonl` |
| Reminders | write | pushed to the apps at the time (and shown in every chat's history) |
| Wakeups | write | `schedule_wakeup`: a prompt the agent runs a proactive turn on at a time, once or every N minutes; `list_reminders` / `cancel_reminder` cover both |
| Heartbeat | config | `[heartbeat]`: random roughly-hourly proactive turns inside an active window (exponential gaps, clamped), persisted across restarts |
| Eli (the LLM inbox) | message | `contact_eli` (or an `@ELI:` line) from any turn: recorded with full context, Eli notified, the task stops |
| You | message | `send_message` from any proactive turn (scheduled check, wakeup, heartbeat); `end_conversation` per the user-level prompt |

A message sent while a turn is running (`Agent.submit`, from any client) is handed to that turn rather than waiting:
the API engine folds it into the next tool-result message; the Agent SDK engine streams its input and writes it
to the CLI's stdin as a further user message, which Claude Code takes into account at its next step (added
2026-11-11 after messages from the phone piled up until the turn ended). Either way it is recorded, announced to
the other clients, and shown with the `MID_TURN_PREFIX` stripped.

Every proactive turn starts with `config.WAKEUP_RULE` (read today's checklist and the running Toggl entry before
deciding anything), prepended in `Agent.run_check`. The agent asked for this itself on 2026-11-11 so the rule does
not depend on its memory; edit the constant, not the agent's memory, to change it.

The standing instructions and user-level system prompt are the Markdown files in `instructions/`; edit
them and the next turn uses the new text. Drop one file per habit into `instructions/habits/` (see the
README and example there) and the scheduled checks compare the data against them.

### Engine: Agent SDK on the subscription, or the API

`llm.provider` in config.toml picks the engine; everything else (tools, prompt, memory, history store, apps)
is the same either way, and you can switch back and forth with a restart:

- `agent_sdk` (current): the Claude Agent SDK runs each turn through Claude Code's harness, **billed to the
  Claude subscription** (the `claude` login on this Mac; the server's API key is hidden from the subprocess).
  Our tools are exposed to it over an in-process MCP server; Claude Code's own tools are off. History lives in
  the SDK's resumable session; every block is still mirrored into the conversation store, so the apps, the
  thinking pane and transcripts work unchanged. When the CLI reports the subscription window at
  `llm.api_fallback_utilization` (90%) or rejected, turns run over the API until the window resets; the
  SDK sessions then resume and are caught up on what the API handled (see "Two threads" below). `/health` → `engine` shows which is in use. Only the stable prompt is the
  session's system prompt; the per-turn context (time, reminders, document roots, memory index, engine state)
  is sent as a "SITUATION" block ahead of the user's text in each turn's message, because a resumed session
  keeps the system prompt it was created with. When the stable prompt changes (code, standing instructions),
  the next turn starts a fresh session with a recap.
- `anthropic`: the Messages API with the API key, the original loop.
- `openai_compat`: a local open-weight model.

### Model

`claude-opus-5-5` with adaptive thinking, `effort = "high"`, prompt caching on the stable system block and
the conversation history, and Anthropic's server-side refusal fallbacks (beta) switched on, which route
a safety-classifier refusal to another Claude model instead of returning an error; set `fallbacks = false`
to turn that off. To swap in an open-weight model served by Ollama / vLLM / LM Studio, set
`provider = "openai_compat"` and `openai_base_url` (see `config.example.toml`). The conversation history
is kept in Anthropic's content-block shape and translated at the provider edge, so a switch mid-life keeps
all memory and history.

### Fast mode: only while you are waiting

Fast mode (the same model at up to ~2.5x the output speed, at twice the price) is on when, and only when, the
turn is a synchronous conversation with you: `mode` `chat` (apps, web page, Shortcut, Telegram, `ea chat`) or
`voice` (a phone call). Everything the agent does on its own runs at standard speed: scheduled checks, wakeups,
heartbeats, Otter and document-change ingestion, `ea run` tasks, worker results and the workers themselves. The
decision is one line in `Agent._handle` (`ea/agent/core.py`); `llm.fast_when_chatting = false` turns it off.

- **Agent SDK engine**: each turn passes `{"fastMode": true|false}` to the CLI as flag settings
  (`AgentSDKRunner._options`), the only layer the CLI accepts it from under the SDK. The log line
  `agent sdk turn: fast mode on|off` is what the CLI reported for the turn. A message you send in the middle of
  a background turn does not speed that turn up (the mode is fixed when the CLI starts); the reply to it does.
- **API engine / fallback**: `speed="fast"` on each model call of the turn (`AnthropicProvider.complete(fast=True)`);
  a background turn switches to fast from the moment a message from you is folded into it. A 429 from fast
  mode's own rate limit is redone once at standard speed.
- **Cost on the subscription**: fast mode is not part of the plan; it is paid from the account's *extra usage*
  credits (claude.ai → Settings → Usage), up to the *monthly spend limit* on the same page. With no credits
  left, or the month's limit reached, the CLI still says "on" but serves standard speed and the log says `fast mode not served this turn: Fast mode disabled · usage credit limit reached`.
### Two threads: one for chat, one for background work

Fast mode and standard speed cannot share a prompt cache. On one session, every switch between a chat turn
(fast) and a background turn (standard) rewrote the whole conversation to the cache at the new speed; with a
conversation of 200k to 900k tokens that is $3 to $15 a switch at fast-mode prices. Replaying the real history
of 6 to 14 Nov 2026 priced fast mode on one session at $44 to $72 a day (2026-11-15). So under the Agent SDK the
main conversation runs as **two sessions with the same history** (`ea/llm/agent_sdk.py`):

- `sdk_session:main` (state.db key-value): background turns, standard speed. This is the original session.
- `sdk_session:main:fast`: chat and voice turns, fast mode. Created on the first chat as a fork of the other
  (the whole history, raw), which costs one full cache write at fast prices, once.
- **Catch-up.** Neither session sees the other's turns, so each keeps a pointer (`sdk_seen:main`,
  `sdk_seen:main:fast`) to the last stored message it knows about, and a turn's message starts with a block
  "WHAT HAPPENED OUTSIDE THIS THREAD": every stored message after the pointer (`ea/agent/catchup.py`). The
  user's words and the agent's own words are replayed in full; tool calls and tool results are cut to 300
  characters each (they are most of the bulk; the agent can call the tool again or read the transcript); the
  other thread's private thinking is left out. Turns that ran over the API during a subscription fallback
  are caught up the same way. Log: `fast lane catches up on N characters from outside it`.
- **Keep-alive.** The cache lasts 60 minutes after its last use. A loop in the scheduler (`Scheduler._keepalive`
  → `Agent.keep_fast_warm`) re-reads the chat session's cache `llm.fast_keepalive_minutes` (55) after its last
  model call: a throwaway fork of the session answers a one-line server message in fast mode with the same
  prompt, tools and options as a chat turn. The fork is not saved, no tool is run for it, and the chat session
  does not see it. A read costs $0.40 per million tokens ($0.08 at 200k), against $16 per million to rewrite.
  It stops `llm.fast_keepalive_max_idle_hours` (36) after your last chat, about where the pings have cost as
  much as one rewrite, and it is skipped once the cache has already expired (the Mac was asleep). Log:
  `fast cache keep-alive: read N tokens, wrote M, about $X`; in `ea usage` these calls are fast-speed rows
  (`mode = keepalive` in `llm_usage`). `fast_keepalive_minutes = 0` turns it off.
- **What the two threads do not share.** The same events reach each thread in a different form: its own turns
  whole (thinking, full tool results), the other's as the catch-up. Each thread is also compacted by the SDK
  on its own schedule, so their summaries of old history differ. Workers are unaffected (own sessions,
  standard speed).
- Verified live 2026-11-15 on a throwaway data folder: after a background turn the next chat read its cache
  (28,533 tokens read, 1,582 written) where one session rewrote everything; each thread answered correctly
  about the other's turn; the keep-alive read 30,115 tokens at fast speed for $0.014 and left no session file.

### Usage by speed

Every model call on either engine is recorded in `state.db` (`llm_usage`, written by `ea/usage.py`): the speed
the API says it *served* the call at (`usage.speed`, so a refused fast request counts as standard), the model,
the kind of turn, the token counts (input, cache read, cache write, output) and a cost estimate at list prices.
`python -m ea usage` prints fast vs standard for today, 7 days, 30 days and since tracking began (2026-11-14),
with each one's share of tokens and of cost; `/health` → `usage_30_days_by_speed` has the 30-day totals.
The cost is an estimate, not a bill: on the subscription only the fast calls are charged (to extra-usage
credits), standard calls are covered by the plan; over the API key everything is charged. The report's last
line per period says how much was actually charged. Prices are the `PRICES` table in `ea/usage.py`; a model
not listed there is counted in tokens and shown with cost `?`. Under the Agent SDK the counts come from the
API stream events the CLI passes through (`message_start` / `message_delta`), one row per model call.

### Parallel workers

The conversation with the user is single-threaded, but the agent can run work in parallel: `delegate` starts a
background worker with the same tools on an isolated thread (up to `llm.max_workers`); its written result comes
back to the main agent as a message. `workers_status` and `cancel_worker` manage them; `/health` lists the
running ones and the app shows them under the chat.

### Web browsing

The agent has a headless Chromium (Playwright): `web_search`, `web_open` (a page as text, paged), `web_more`,
`web_links`, `web_click`, `web_fill`. JavaScript runs; nothing persists between runs; it is told never to enter
passwords or pay. `python -m playwright install chromium` once after installing; `sources.web_browsing = false`
disables it. The main agent and each background worker get a Chromium of their own (a *context*, keyed by the
calling thread's `ea_context`), each driven from a dedicated thread: Playwright's sync objects are bound to the
thread that made them, and tool calls arrive on arbitrary threads. A worker's browser closes when the worker
finishes; a Chromium that died is relaunched on the next call.

### Roam and Otter

- **Roam**: drop an export (JSON from Export All, or the Markdown zip/files) in `data/roam/`; the agent searches
  pages and blocks, reads pages as outlines, lists recent journals and backlinks. Re-indexed on change.
  `sources.roam_backup_git` names the daily GitHub backup, pulled every `roam_sync_hours`.
- **Otter.ai**: with `OTTER_EMAIL`/`OTTER_PASSWORD` set, every new recording is pulled on a schedule and the
  agent reads it in its own turn, filing tasks, decisions and memories as `STANDING_INSTRUCTIONS.md` says.
  The whole history is searchable with the `otter_*` tools.

### Channels

- **The Atlas apps** (iPhone + Mac, `apps/Atlas/`): the main text channel. Proactive messages arrive as push
  notifications (APNs; needs a paid Apple developer account and `APNS_*` in `.env`), the chat streams replies,
  and every open copy stays in sync through `GET /api/events`. Setup in `apps/Atlas/README.md`. Beside the chat
  is the shared workspace (a whiteboard plus named documents) edited in Literal's editor with Mermaid diagrams,
  and a thinking pane.
- **Telegram** (optional, legacy): make a bot with @BotFather, put the token in `.env`, your numeric user id
  in `channels.telegram_allowed_user_ids` (ask @userinfobot). Text and voice notes (transcribed with
  OpenAI or Deepgram if a key is set, or local faster-whisper). Anyone not on the list is ignored.
- **iPhone one-button / Siri "Atlas …"**: `ios/SHORTCUT.md`. Needs the agent reachable over HTTPS
  (`channels.public_base_url`) and `EA_WEB_TOKEN`.
- **Web chat** for the laptop: `http://<host>:8800/` (paste the token once).
- **Phone calls**: the agent calls you (`call_user`) from a Twilio number whose Voice webhook is
  `https://<public_base_url>/twilio/voice`; you can also call it. Twilio does speech recognition and the
  voice; the agent answers in short spoken sentences. Signature-checked, caller allow-listed.
- **Scheduled checks** (`[[checks]]` in config.toml): proactive turns at set times. Nothing is sent unless
  the agent decides to; "all on track" ends silently.
- **Wakeups**: the agent schedules turns for itself (`schedule_wakeup` with a time, a prompt to itself, and an
  optional repeat), e.g. "at 15:10 check whether the 14:00 focus block happened". They run like checks.
- **Heartbeat** (`[heartbeat]` in config.toml): random, roughly hourly turns in the active window (exponential
  gaps, clamped to min/max minutes) where the agent decides whether anything deserves a look. The next time is
  persisted, so restarts do not fire it; `python -m ea run-check heartbeat` runs one by hand.

### Drafts are never lost

Everything you type is saved as you type: the web chat composer goes to `localStorage` on every keystroke and
is restored on load (cleared only once the server accepted the message; a failed send puts the text back); the
apps' composer (`ChatStore.draft`) lives in `UserDefaults` and comes back if a send fails; the "New document"
name and the Settings fields persist the same way; the workspace editor saves to the server 0.8 s after typing
stops. Only the access-token field is excluded.

### Security notes

- The local servers have no auth of their own; they are only reachable on localhost, and from the cloud
  only through the bridge (bearer token, Tailscale-only bind, explicit allow-list of the few tracker writes
  the agent needs, side-effecting reads blocked).
- Inbound to the agent: push is outbound-only from the server; Telegram (if used) is pull; `/api/message` needs the bearer token; Twilio
  webhooks must carry a valid signature and come from an allowed number. With `EA_WEB_TOKEN` unset the
  endpoint refuses everything.
- Secrets live in `data/.env` (mode 600) or the environment, never in config or code. `data/` is gitignored.
- The agent's writes are limited to the Project Tracker and its own memory/reminders; every tool call is logged.

### Deploying elsewhere

- **Google Cloud**: `deploy/gcp/README.md` (small VM + Docker + Tailscale; the Mac runs the bridge). The Docker
  image needs a copy of `agent-coding/shared/` and `EA_SHARED_DIR` pointing at it (the LLM standard above).
- **Mac mini later**: copy the repo and `data/`, set `sources.bridge_url = ""`, run `deploy/macmini/install.sh`.
- Same `deploy/docker-compose.yml` works in both places.

## Files

| Path | Purpose |
|---|---|
| `ea/__main__.py` | CLI: `serve`, `chat`, `check`, `run-check`, `reindex`, `usage`, `roam`, `run`, `notify`, `push-test`, `otter`. Header comment says where everything lives |
| `ea/config.py` | `config.toml` → dataclasses; secrets from the environment, then `data/.env`; `EA_DATA`, `EA_CONFIG`, `EA_INSTRUCTIONS` overrides |
| `ea/contact.py` | The shared LLM inbox: preamble on every system prompt, the `contact_eli` tool, `check()` after every response |
| `ea/agent/core.py` | The loop: one turn in, tool calls until the model stops, one reply out; mid-turn messages; SDK vs API dispatch |
| `ea/agent/prompt.py` | System prompt: stable cached block + volatile block (clock, channel, memory index, reminders) |
| `ea/agent/conversations.py` | History, transcripts, reminders/wakeups, key-value state (SQLite `state.db`) |
| `ea/agent/workers.py` | Background workers (`delegate`): isolated turns in parallel, results handed back to the main agent |
| `ea/llm/base.py` | Neutral shapes: `ToolSpec`, `LLMResponse`, `Provider` |
| `ea/llm/anthropic_provider.py` | Messages API: adaptive thinking, effort, caching breakpoints, refusal fallbacks |
| `ea/llm/agent_sdk.py` | Claude Agent SDK engine: our tools as an in-process MCP server, resumable sessions (a fast one for chat, a standard one for background work), the cache keep-alive, subscription usage + API fallback |
| `ea/agent/catchup.py` | The catch-up one SDK session gets about the other's turns: text in full, tool calls and results shortened |
| `ea/llm/openai_compat_provider.py` | Ollama / vLLM / LM Studio translation layer |
| `ea/llm/factory.py` | Picks the provider from `llm.provider` |
| `ea/tools/` | `registry.py` + `data_tools`, `tracker_tools`, `memory_tools`, `assistant_tools`, `otter_tools`, `roam_tools`, `web_tools`, `worker_tools`, `workspace_tools` |
| `ea/connectors/` | `http.py` (router: bridge vs localhost), `toggl`, `focus_monitor`, `sleep_bandit` (+Oura), `exercise`, `calendar`, `project_tracker`, `otter` (Otter.ai API + Logseq archive), `roam`, `browser` (Playwright) |
| `ea/memory/store.py` | Markdown memories + FTS5 index with dated provenance |
| `ea/channels/` | `web.py` (apps/Shortcut endpoints, chat page, Twilio webhooks), `push.py` (APNs), `events.py` (live feed, presence), `twilio_voice.py`, `telegram.py` (optional) |
| `ea/scheduler/jobs.py` | Reminders, self-scheduled wakeups, random heartbeat, scheduled checks, Otter ingestion, Roam backup sync, the fast-cache keep-alive loop |
| `ea/usage.py` | Tokens and estimated cost per model call, split by speed (fast vs standard); the report behind `ea usage` |
| `ea/workspace.py`, `ea/images.py`, `ea/integrations.py`, `ea/stt.py` | Shared documents, image attachments, integration status for `/health`, speech-to-text |
| `instructions/` | `STANDING_INSTRUCTIONS.md`, `USER_SYSTEM_PROMPT.md`, `MEMORY_CONVENTIONS.md`, `habits/*.md` (one file per habit + README and example) |
| `apps/Atlas/` | SwiftUI iPhone + Mac apps (`project.yml` → xcodegen; `build.sh` builds, signs and relaunches the Mac app; own README) |
| `bridge/` | `mac_bridge.py` + `com.elityre.ea-bridge.plist` + `install_bridge.sh`: lets a cloud agent reach the Mac's local servers over Tailscale |
| `deploy/` | `Dockerfile`, `docker-compose.yml`, `gcp/` (VM scripts + README), `macmini/` (launchd plist + `install.sh`) |
| `ios/SHORTCUT.md` | Action button / Siri setup |
| `scripts/import_tracker_settings.py` | Copies the exercise export URL and calendar feeds from the Project Tracker's config into `data/.env` |
| `tests/` | pytest suite (no network, scripted model); `test_llm_contact.py` covers the LLM inbox on both engines, `test_fast_mode.py` which turns are fast |
| `config.example.toml`, `.env.example` | Committed templates for `config.toml` and `data/.env` (both gitignored) |
| `NEXT_STEPS.md` | Running log of what is done, deferred and next (shown at the start of every Claude Code session per `CLAUDE.local.md`) |
| `data/` | (created, gitignored) the agent's whole state: `memory/`, `transcripts/`, `state.db`, `otter.db`, `roam.db`, `roam/`, `workspace/`, `images/`, `.env` |

## Runtime locations

- **Installed copy**: this repo, run in place by `~/Library/LaunchAgents/com.elityre.executive-assistant.plist`
  (`.venv/bin/python -m ea serve`, `RunAtLoad` + `KeepAlive`). The Mac app is `/Applications/Atlas.app` (login item).
- **Config**: `config.toml` in the repo (`EA_CONFIG` overrides); prompts in `instructions/`.
- **Secrets**: `data/.env` (mode 600) or the environment.
- **State**: `data/` (`EA_DATA` overrides): memory, transcripts, `state.db`, `otter.db`, `roam.db`, workspace, images.
- **Logs**: `~/Library/Logs/executive-assistant.log` (launchd stdout+stderr, every line timestamped). Copies
  installed before 2026-11-07 still log to `data/ea.log` until `deploy/macmini/install.sh` is rerun. The bridge
  logs to `~/Library/Logs/ea-bridge.log`.
- **LLM inbox** (shared across projects): `~/Library/Application Support/LLMInbox/` (messages, config, debriefs);
  its log is `~/Library/Logs/llm-inbox.log`.
- **Web chat / API**: `http://127.0.0.1:8800/` (`/health`, `/api/events`, `/api/message`, …).
- **Mac app**: token in `~/Library/Application Support/Atlas/token` (0600), the rest in `UserDefaults`
  (`com.elityre.atlas`); diagnostics in `~/Library/Logs/Atlas/`.

## Common tasks

```bash
cd ~/Documents/agent-coding/executive-assistant-agent
/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt   # done already
.venv/bin/python -m playwright install chromium   # once, for web browsing
cp config.example.toml config.toml           # edit: agent_name, timezone, apns_bundle_id, checks
cp .env.example data/.env                    # fill in ANTHROPIC_API_KEY, TOGGL_API_TOKEN, EXERCISE_EXPORT_URL, TELEGRAM_BOT_TOKEN, EA_WEB_TOKEN
.venv/bin/python scripts/import_tracker_settings.py   # copy exercise URL + calendar feeds from the tracker's config

# install / restart the background service (idempotent; this IS the restart command)
deploy/macmini/install.sh
launchctl kickstart -k gui/$(id -u)/com.elityre.executive-assistant   # restart only, same plist
tail -f ~/Library/Logs/executive-assistant.log
launchctl bootout gui/$(id -u)/com.elityre.executive-assistant        # stop

# test without the real trigger
.venv/bin/python -m pytest -q                # 124 tests, no network, scripted model (incl. the LLM inbox on both engines, fast mode per turn kind, usage by speed, the two SDK threads and keep-alive)
.venv/bin/python -m ea check                 # what is configured and reachable (every source probed)
.venv/bin/python -m ea check --llm           # ...plus one real model call ("ready")
.venv/bin/python -m ea run-check morning     # run one scheduled check by hand (or `heartbeat`)
.venv/bin/python -m ea notify "hello"        # a message through the running server to every app/page
.venv/bin/python -m ea run "look at today"   # give the running agent a background task
.venv/bin/python -m ea push-test             # Apple push to the registered devices
llm-inbox test                               # fake inbox message end to end (shared tool)

# run by hand instead of launchd
.venv/bin/python -m ea chat                  # talk to it in the terminal
.venv/bin/python -m ea serve                 # apps/Shortcut/web endpoint (:8800) + push + scheduler (+ Telegram if configured)

# data upkeep
.venv/bin/python -m ea reindex               # after editing memory files by hand
.venv/bin/python -m ea otter --all           # first full Otter pull; --sync for the newest
.venv/bin/python -m ea roam                  # index the Roam export in data/roam/
.venv/bin/python -m ea usage                 # tokens and estimated cost, fast mode vs standard; then the subscription usage readings from the CLI

# the Mac app
cd apps/Atlas && ./build.sh                  # xcodegen + build + sign + relaunch (see apps/Atlas/README.md)
```

The Project Tracker, Focus Monitor and Sleep Bandit must be running (they are, as login items). If the tracker is closed, the agent can start it itself (`tracker_open`).

## Settings that matter

- The `claude` login on this Mac (the subscription the `agent_sdk` engine bills); `ANTHROPIC_API_KEY` in
  `data/.env` is the fallback when the window is used up.
- The tracker's agent API (`/api/agent`) and Sleep Bandit's agent routes are additions in those repos
  (`project-tracker-and-organizer` has it in `tracker/agent_api.py`, see its README "Agent API"; `sleep-bandit`
  gained `GET /api/agent/summary`, `GET /api/oura/collection` and `oura.fetch_collection`). A running copy
  started before they were added needs a restart (quit and reopen the Task Tracker app;
  `launchctl kickstart -k gui/$(id -u)/com.elityre.sleep-bandit`).
- Nightblock (00:00–06:00): localhost and the Anthropic API pass through; Toggl, Otter, Apple push and Twilio
  hosts need `nightblock config-add direct_hosts` / `allow` (the command is in `NEXT_STEPS.md`);
  `script.google.com` (exercise export) stays blocked overnight.
- macOS notifications for the Mac app need a stable code signature (`build.sh` signs with the Apple Development
  certificate from Xcode → Settings → Accounts); System Settings → Notifications → Atlas → Allow; add the app to
  Login Items.
- Deferred by Eli (2026-11-06): the public HTTPS URL (Tailscale Funnel), Twilio calls, Apple Developer Program /
  iPhone push. Steps are in `NEXT_STEPS.md`.
- The LLM inbox's own settings (`NTFY_TOPIC`, email) are in `~/Library/Application Support/LLMInbox/config.env`.

## Gotchas learned

- **Fast and standard speed do not share a prompt cache, and the cache dies after an hour.** One session used
  for both rewrote the whole conversation at every switch ($3.27 for one short chat reply on 2026-11-14, with
  204,551 tokens written). Hence two sessions plus a keep-alive; see "Two threads". If chat turns get dear
  again, look in `llm_usage` for fast rows with a large `cache_write_1h` and no `cache_read`: that is a cold
  start (keep-alive off, Mac asleep over an hour, or the standing prompt changed and the session restarted).
- **`tests/test_tracker_open.py::test_tracker_tools_point_at_tracker_open_when_the_app_is_down`** fails when
  the tracker app is not in the state the test assumes (seen 2026-11-15, unrelated to the change being made).

- **Fast mode "on" does not mean fast.** Under the Agent SDK the CLI's init message says
  `fast_mode_state: "on"` as soon as the setting is accepted; whether the request was served fast is only in
  the response's `usage.speed`. On 2026-11-14 a probe turn reported "on", then a `fast-mode-overage-rejected`
  notification ("usage credit limit reached") and `speed: "standard"`: the account had $199 of credits but
  its monthly spend limit ($50) was already used up, which gives the same message as having no credits. Over the API key the same day `speed: "fast"` came back. Also: with `setting_sources=[]` a
  `fastMode` in the user's own settings is ignored (`sdk_opt_in_required`); it must come through `settings=`.

- **A resumed Claude Code session ignores a changed `system_prompt`.** Verified 2026-11-12 with a two-turn probe
  (prompt "the code word is APPLE", then resume with "BANANA": the model quotes APPLE). For five days the agent's
  per-turn context (time, reminders, memory index, document roots) only refreshed when the CLI happened to
  rebuild its context, which is why it said it could not see `source_docs` after the prompt named the folder.
  Now the volatile block rides in the user message and the stable block is hashed per thread
  (`sdk_system_sig:<thread>`); a change restarts the session with a recap.

- **Playwright "Cannot switch to a different thread" (greenlet), then every web call fails with TargetClosedError.**
  Playwright's sync API may only be used from the thread that started it. Under the Agent SDK engine every tool
  call runs on an `asyncio.to_thread` pool thread, so the browser worked only while one pool thread happened to
  serve all calls; a worker running web research alongside the main agent put calls on a second thread and broke
  the session for everyone (2026-11-10 and 2026-11-12). Fixed 2026-11-12: each context's browser lives on its
  own thread and calls are marshalled to it (`ea/connectors/browser.py`), the SDK engine tags its dispatch
  threads with the turn's context (`AgentSDKRunner._dispatch`), and a dead Chromium is relaunched.

- Restart by `launchctl kickstart -k` or `bootout`/`bootstrap`, never `pkill` alone. Without
  `timeout_graceful_shutdown` uvicorn waited forever for open event streams, so a restart left the old process
  alive (still pinging the apps from a dead bus) next to the new one (2026-11-06).
- `no_action_needed` is terminal: the model otherwise kept re-declaring it until the round limit (14 wasted
  calls after the work was done, 2026-11-06).
- Under the Agent SDK, breaking out of `query()` leaves its generator running; with images the turn goes through
  `ClaudeSDKClient.receive_response` instead. A stale session id is dropped and the next turn starts fresh.
- Turns that ran over the API during a subscription fallback are invisible to the SDK session, so the session is
  restarted with a recap of the recent conversation.
- When the model contacts Eli mid-turn, the turn is cut short; the pending `tool_use` blocks are paired with a
  synthetic `tool_result` so the stored history (and a later API-engine turn) stays valid. The SDK's own session
  file may hold a dangling tool call; it resumes or is dropped as above.
- The shared confirm step (`llm_contact.with_confirm`: one forced-choice question before a `contact_eli` call is
  sent) is deliberately not wired in here (looked at 2026-11-10); every `contact_eli` call is sent as is. Agent
  SDK engine: the harness owns the model calls and the tool loop, so there is no place to make a forced-choice
  call on the same conversation or to hand back a "keep working" tool result. API engine: the model is
  `claude-opus-5-5`, which per the API docs answers 400 both to a forced `tool_choice` and to disabled
  thinking, so the decision call could only fail (and fall back to sending). Revisit if `llm.model` becomes one
  that accepts both; the place is between `provider.complete()` and `contact.check()` in `agent/core.py`.
- `.env` lines may carry `# comments` after the value; `config.py` strips them.
- Mac app: the Keychain asked for the login password on every launch of an ad-hoc-signed build, so the token is
  a 0600 file in Application Support on macOS (Keychain on iOS). Ad-hoc signing also breaks notifications
  (`UNErrorDomain error 1`): sign with a stable identity.
- Mac app composer: a vertical `TextField` would not wrap at narrow widths and clipped the text; it is a
  `TextEditor` that grows to eight lines.
- The exercise export (`script.google.com`) cannot be reached through nightblock overnight; the Toggl desktop
  app (native Swift) may skip the proxy and needs its hosts allowed directly.
- Otter ingestion marks a recording seen before the agent reads it, so a crash mid-turn cannot loop on it; the
  first run marks all existing recordings as seen (history is not news).
- The shared `llm_contact.py` is imported from `~/Documents/agent-coding/shared` (`EA_SHARED_DIR` overrides); a
  Docker image without it fails at import on purpose rather than running without the preamble.
