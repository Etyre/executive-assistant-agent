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

The second shared tool, `note_to_eli` (2026-11-20), is in the registry too. It leaves Eli a message and the turn
carries on: `contact.check()` records the call once (`via: note`, `halts: false`, same context as a contact) and
returns nothing, and the registry handler answers it with the shared "Delivered to Eli … carry on" text on both
engines. A note is never the turn's reply and never ends it.

### What the agent can see and do

| Source | Access | How |
|---|---|---|
| Toggl Track | read | by default the Focus Monitor's synced copy (`/api/toggl/current`, `/api/toggl/entries`; it polls Toggl every 5 min, so "current" can lag that much); `sources.toggl_source = "toggl"` calls Toggl API v9 directly with `TOGGL_API_TOKEN`. Switched 2026-11-11: the agent's own polling kept hitting Toggl's hourly quota, shared with the Focus Monitor |
| Focus Monitor | read | `GET /api/metrics`, `/api/day` on :8790 (daily focus spans, distractions, totals) |
| Oura ring | read | through Sleep Bandit's OAuth token: new `GET /api/oura/collection?name=daily_readiness&days=N` on :8777 (sleep, readiness, activity, stress, heartrate…) |
| Sleep Bandit | read | new `GET /api/agent/summary?days=N` on :8777: nights, metrics, tonight's checklist, verdicts, HRV readiness status (no planning side effect); the connector cuts it down (`ea/connectors/sleep_bandit.py`) and returns per-dose tables only for one named intervention |
| Exercise log | read | the exercise-tracker Apps Script `?action=export` (`EXERCISE_EXPORT_URL`) |
| Otter.ai transcripts | read | new recordings through Otter's unofficial API (`OTTER_EMAIL`/`OTTER_PASSWORD`, no third party), plus the 2024+ archive the Roam extension wrote into the Logseq graph (`sources.otter_logseq_graph`); both merged into `data/otter.db` with FTS5 search (`otter_recent`, `otter_search`, `otter_transcript`); `python -m ea otter --all` for the first full pull |
| Calendar | read + write | read: the same iCal feed(s) the tracker uses (`CALENDAR_ICS_URLS`), any date range up to 62 days; falls back to the tracker's single-day `GET /api/calendar`. Write (`calendar_create_event`, `calendar_update_event`, `calendar_delete_event`, `calendar_writable`): a Google Apps Script web app that runs as you (`deploy/calendar_script/`, address in `CALENDAR_WRITE_URL`, shared secret in `CALENDAR_WRITE_TOKEN`); read-only until both are set. The script is authorised for one thing only, Google's `calendar.events.owned` scope (see, create, change and delete events on calendars you own; no sharing, settings or subscribed calendars), never adds guests or sends notices, and refuses to change or delete an event that has other guests. That scope cannot list calendars, so the agent names a calendar to the script by the id in its feed address |
| Google Sheets | read | sheets you open to the agent one by one (2026-11-16: the responses of the "Intentionality and work block record" form and of "What did I make or produce"): `sheets_available`, `sheet_records`. Each sheet has its own copy of `deploy/sheet_reader_script/` bound to it, which only returns rows; `READ_SHEETS` lists them as `name=/exec URL` and `READ_SHEETS_TOKEN` is the one shared secret |
| Project Tracker | read + write | through the tracker's **agent API** (`/api/agent/*`, sends `X-Agent-Name` and the optional `AGENT_API_TOKEN`): outline and task search, tasks/projects created with agent attribution, signed notes on any item, quick-add, the day's scheduling context (free windows, prioritised candidates, rules), **set the schedule** block by block (pinned against the tracker's re-planner, with what did not fit returned), steer a day in prose (instructions the tracker re-plans with), rebuild, unpin; plus feed, complete/update task from the plain API. The daily checklist is read-only and comes back compact (per row: today, last met, consecutive misses, colour from its steps-to-yellow rule; `days` adds recent raw values, `full` the whole sheet), because the full sheet is ~50 KB, more than the SDK passes back as a tool result. `tracker_open` starts the Project Tracker app (`sources.tracker_app`, opened with `open -g` so it does not take focus) when its API is down; every tracker tool says so when the app is not answering. Only on the Mac itself, not through the bridge Tags and metadata go through the same API: `tracker_set_task_meta` (the tags string as typed, `#p1 #due:fri #reading`, or named fields; estimate; timer name) and `tracker_set_project_meta` (subtitle, Toggl project), each recorded by the tracker as the agent's signed note with its `why`. |
| Memory | read + write | `data/memory/*.md` with `sources` (conversation dates + channel) and `edits` (dates) in the frontmatter; FTS5 search; transcripts in `data/transcripts/YYYY-MM-DD.jsonl` |
| Reminders | write | pushed to the apps at the time (and shown in every chat's history) |
| Wakeups | write | `schedule_wakeup`: a prompt the agent runs a proactive turn on at a time, once or every N minutes; `list_reminders` / `cancel_reminder` cover both |
| Heartbeat | config | `[heartbeat]`: random roughly-hourly proactive turns inside an active window (exponential gaps, clamped), persisted across restarts |
| Eli (the LLM inbox) | message | `contact_eli` (or an `@ELI:` line) from any turn: recorded with full context, Eli notified, the task stops. `note_to_eli`: recorded and notified the same way, the task carries on |
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
- **On the server the CLI must be told the account has fast mode.** Signed in with a `claude setup-token` token it
  never caches `penguinModeOrgEnabled` in `~/.claude.json`, reports fast mode `'off' (preference)`, and serves
  standard speed: every chat turn from the move (2026-11-17) until the next day. `know_fast_mode()` in
  `ea/llm/agent_sdk.py` writes the value before a fast turn when that token is in use. The sign in the log is
  `fast mode requested but the CLI reports 'off'`; `SELECT speed, mode FROM llm_usage` in `state.db` shows it too.
- **A keep-alive that is not served fast is tried once per window**, not every 30 seconds until the cache expires
  (`sdk_fast_ping_missed`); each try reads the whole conversation.
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
- **Switching back to one thread.** `llm.fast_own_thread = false` and a restart: every turn then runs on the
  original session, which is first caught up on what the chat session did. Set it back to `true` and the chat
  session resumes and is caught up in turn. Nothing is lost either way, because both sessions are fed from
  the conversation store; what crosses over arrives in catch-up form (tool results shortened). With one thread
  and `fast_when_chatting` still on, each speed switch rewrites the cache again (the dear case above);
  `fast_when_chatting = false` as well gives one thread at standard speed, no credits spent.
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

`ea usage` also prints, and the agent itself reads through its `model_usage` tool (`ea/tools/assistant_tools.py`):
a per-day table of what was charged beyond the plan (split into chat, keep-alive and other, with the number
and cost of *cold starts*: fast calls that rewrote the conversation to the cache instead of reading it), and
where the money goes by component (uncached input, cache reads, cache writes, output) and by kind of turn.
Every call is kept for good (one row each in `llm_usage`), and the agent's `model_usage_query` tool
(`usage.query`) reads any date range grouped by any of day, hour, speed, mode, model, billing, channel,
engine, thread or single call, with tokens and dollars per component, so it can aggregate its own way.

**The agent watches its own costs (Eli, 2026-11-15).** Its memory `model-costs` (`data/memory/model-costs.md`)
holds the background: billing, the two threads, the keep-alive, the estimate, how to read the tables, the
settings. `Scheduler.watch_costs` (every poll) starts two kinds of proactive turn: `[cost alert: ...]` when the
charged spend of the last 24 hours passes `llm.cost_alert_usd_per_day` (150), looked at every
`llm.cost_alert_check_hours` (1; the check is a database query, no model call; at most one alert per 24 hours), and `[cost review: ...]`
every `llm.cost_review_days` (7), both of which always message Eli. The review is timed from `cost_review_next`
in state.db (first one 7 days after the first poll), inside the heartbeat's active window, so a sleeping Mac
delays it instead of skipping it.

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

### Food ordering

Built 2026-11-19 (Eli: a meal ordered every day without him thinking about it). **What runs today: the agent
chooses, Eli orders.** The agent browses Grubhub's menus signed out, picks a dish that meets the requirements in
its memory, and sends Eli one line and nothing else (he asked for just a link, 2026-11-19): `[dish · restaurant](url)`.
The apps show the label as a tappable link (`Linkified` in `ChatView.swift`; a notification shows the label alone)
that opens the restaurant in the Grubhub app on his phone, where he adds the dish and pays. When the agent does this is its own daily wakeup (`schedule_wakeup` with
`every_minutes`), set when Eli tells it the time.

Ordering by the agent itself was the goal and is built below, but **no site allows it**: Uber Eats answers an
automated browser with a bot check everywhere; Grubhub lets it read menus but answers its sign-in with a "Press &
Hold" robot check that fails even when Eli presses it himself, and Google refuses its sign-in outright (all
2026-11-19). Getting past those means disguising the browser, which this project does not do. With
`[food] max_total_usd = 0` (today) the tools are the browsing ones only; above 0 `food_place_order` and
`food_orders` appear, for a site where the browser can be signed in.

- **Where it runs.** A Chrome with a profile of its own (`[food] profile`, never Eli's everyday Chrome) on the
  Mac, headless, driven by the bridge (`POST /mac/food/<step>`). So choosing a meal needs the Mac awake. (For a
  site that allowed it, Eli would sign in once himself with `bridge/food_signin.py`; the agent never sees a password.)
- **How the agent uses it** (`ea/connectors/site_browser.py`, `ea/tools/food_tools.py`). Each page comes back as
  text plus a numbered list of what can be clicked or typed into: `food_view` (with `find` for long menus),
  `food_open`, `food_click`, `food_type`, `food_scroll`. What to order and when is in the agent's memory and its
  own wakeups; tell it in conversation.
- **What the browser refuses, whatever the model asks:** any host but `[food] hosts`; typing into a password or
  card field; pressing a button that completes a purchase.
- **Placing the order** (`ea/food.py`, on the Mac): `food_place_order(expected_total)` presses the button only when
  ordering is on, fewer than `max_orders_per_day` were placed today, the checkout page shows exactly one total and
  one purchase button, the total equals `expected_total` and is within `max_total_usd` (fees and tip included), and
  the page shows the address in `FOOD_ADDRESS_CONTAINS` (`data/.env`) when that is set. The attempt is written to
  `data/food_orders.jsonl` **before** the press, with a screenshot before and after in `data/food/`, so an attempt
  whose outcome is unknown still uses up the day and is never repeated. `food_orders` shows the record.
- The card is whatever is saved on the Grubhub account; a card with its own limit is the cap that holds if this
  code is wrong.

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
  and a thinking pane. The agent can open the workspace pane (`workspace_open`, `document_open`) and close it
  (`workspace_close`); both travel as a `ui` event to every open app. Design rule (Eli, 2026-11-17): whatever the
  agent can do in the app it can also undo, so a tool that opens, shows, adds or pins comes with its opposite. The
  pairs: `workspace_open`/`workspace_close`, `thinking_pane` (show true/false), `tracker_open`/`tracker_close`, `tracker_archive_task` and `tracker_archive_project` (each with `restore: true`; the tracker's "delete" is an archive)
  (quits the tracker app: its API, menu bar item and so the agent's tracker and calendar tools, until reopened),
  `memory_archive`/`memory_restore` (with `memory_archived` to list what can come back).
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

### Running on a server

`deploy/server/README.md` is the guide. In short: one Docker container on an Ubuntu server, reachable only over
Tailscale; the Mac runs a small bridge (`bridge/mac_bridge.py`) through which the server reads the Project
Tracker, Focus Monitor and Sleep Bandit, copies the document folders, asks for the tracker to be opened, and
files LLM-inbox messages back into the Mac's inbox. `deploy/server/atlas-server.sh` provisions, deploys, and
moves the agent there (`cutover`) and back (`back-to-mac`); only one copy ever runs. `config.server.toml` is laid
over `config.toml` on the server (`EA_CONFIG_OVERLAY`). When the Mac is asleep the Mac-only tools say so in plain
words and everything else carries on.

- **Mac mini later**: copy the repo and `data/`, leave `sources.bridge_url` empty, run `deploy/macmini/install.sh`.

## Files

| Path | Purpose |
|---|---|
| `ea/__main__.py` | CLI: `serve`, `chat`, `check`, `run-check`, `reindex`, `usage`, `roam`, `run`, `notify`, `push-test`, `otter`. Header comment says where everything lives |
| `ea/config.py` | `config.toml` → dataclasses; secrets from the environment, then `data/.env`; `EA_DATA`, `EA_CONFIG`, `EA_INSTRUCTIONS` overrides |
| `ea/contact.py` | The shared LLM inbox: preamble on every system prompt, the `contact_eli` (stops) and `note_to_eli` (carries on) tools, `check()` after every response |
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
| `apps/Atlas/` | SwiftUI iPhone + Mac apps (`project.yml` → xcodegen; `build.sh` builds, signs, installs into /Applications and relaunches the Mac app; own README) |
| `apps/Atlas/auto_deploy.py`, `install-auto-deploy.sh` | launchd agent `com.elityre.atlas-app-deploy`: builds each new commit of the Mac app and installs it in /Applications once the agent is not mid-turn |
| `bridge/` | `mac_bridge.py` + `com.elityre.ea-bridge.plist` + `install_bridge.sh`: what the Mac does for an agent running on a server (the three local services, document folders, opening the tracker, receiving LLM-inbox records), on the Tailscale address only. `backup_server.py` + `com.elityre.ea-backup.plist` + `install_backup.sh`: the copy of the server's conversations and memory kept on this Mac |
| `ea/food.py`, `ea/connectors/site_browser.py`, `ea/tools/food_tools.py`, `bridge/food_signin.py` | Food ordering: the limits and the order record; the signed-in browser; the agent's tools; the window Eli signs in through |
| `ea/inbox_relay.py`, `ea/docs_mirror.py` | Server deployment only: copy LLM-inbox records to the Mac, and the Mac's document folders to the server, both through the bridge |
| `deploy/` | `Dockerfile`, `docker-compose.yml`, `server/` (`atlas-server.sh`: provision, deploy, move the agent to a server and back; own README; `config.server.example.toml`), `macmini/` (launchd plist + `install.sh`), `calendar_script/` (`Code.gs` + manifest: the Google Apps Script that gives the agent calendar write access; setup steps at the top of `Code.gs`), `sheet_reader_script/` (the read-only script bound to each Google Sheet the agent may read) |
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
- **Backup of the server's state on the Mac** (2026-11-20): `~/Library/Application Support/Atlas/server-backup/snapshots/YYYY-MM-DD/`,
  one folder per day for 30 days (unchanged files are hard links, so a day costs about 30 MB), made at login and every
  six hours by the launchd agent `com.elityre.ea-backup`. It holds the transcripts, memory, workspace, images, a
  consistent snapshot of `state.db` (in `.backup/`), the CLI's session files and the LLM inbox; not the Roam and Otter
  indexes (rebuilt from their sources) and no secrets. `last_ok.json` beside the snapshots names the last backup that
  passed its checks; log `~/Library/Logs/ea-backup.log`. To restore: stop the agent, copy a snapshot's folders back
  into `/opt/atlas/data` (the `state.db` from `.backup/`), owned by user 1000, and start it.
- **Mac app auto-deploy**: agent `~/Library/LaunchAgents/com.elityre.atlas-app-deploy.plist`; state (last installed
  source id) and the clean build folder in `~/Library/Application Support/AtlasAppDeploy/`; log
  `~/Library/Logs/atlas-app-deploy.log`.
- **Logs**: `~/Library/Logs/executive-assistant.log` (launchd stdout+stderr, every line timestamped). Copies
  installed before 2026-11-07 still log to `data/ea.log` until `deploy/macmini/install.sh` is rerun. The bridge
  logs to `~/Library/Logs/ea-bridge.log`.
- **Server deployment**: see `deploy/server/README.md` (code in `/opt/atlas/app`, state in `/opt/atlas/data`). On the
  Mac: server-only secrets in `data/server.env`, the differences in `config.server.toml`, both untracked.
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
.venv/bin/python bridge/food_signin.py            # food ordering: opens a window to sign in to Grubhub (again if it signs out)
.venv/bin/python -m pytest -q tests/test_food.py  # food ordering end to end against a local page, real Chrome, no real order
cp config.example.toml config.toml           # edit: agent_name, timezone, apns_bundle_id, checks
cp .env.example data/.env                    # fill in ANTHROPIC_API_KEY, TOGGL_API_TOKEN, EXERCISE_EXPORT_URL, TELEGRAM_BOT_TOKEN, EA_WEB_TOKEN
.venv/bin/python scripts/import_tracker_settings.py   # copy exercise URL + calendar feeds from the tracker's config

# install / restart the background service (idempotent; this IS the restart command)
deploy/macmini/install.sh
launchctl kickstart -k gui/$(id -u)/com.elityre.executive-assistant   # restart only, same plist
tail -f ~/Library/Logs/executive-assistant.log
launchctl bootout gui/$(id -u)/com.elityre.executive-assistant        # stop

# test without the real trigger
.venv/bin/python -m pytest -q                # 129 tests, no network, scripted model (incl. the LLM inbox on both engines, fast mode per turn kind, usage by speed, the two SDK threads and keep-alive, the cost alert and review)
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
cd apps/Atlas && ./build.sh                  # xcodegen + build + sign + install in /Applications + relaunch (see apps/Atlas/README.md)
apps/Atlas/install-auto-deploy.sh            # (re)install the agent that does the above for every commit, waiting for the agent to be idle
/opt/homebrew/bin/python3 apps/Atlas/auto_deploy.py status   # committed vs installed; `once` runs one check now (end-to-end test)
tail -f ~/Library/Logs/atlas-app-deploy.log
```

The Project Tracker, Focus Monitor and Sleep Bandit must be running (they are, as login items). If the tracker is closed, the agent can start it itself (`tracker_open`).

## Settings that matter

- **Read-only sheets**: for each sheet in `READ_SHEETS`, an Apps Script project bound to that sheet (open the sheet,
  Extensions -> Apps Script) holding `deploy/sheet_reader_script/Code.gs`, deployed as a web app (Execute as: Me,
  access: Anyone), with the Script Property `SHEET_TOKEN` equal to `READ_SHEETS_TOKEN`. Its Google permission is
  `spreadsheets.currentonly`: that one spreadsheet only. Google has no scope that is both one-file and read-only,
  so "read-only" is enforced by the script having no code that writes; the alternative, `spreadsheets.readonly`,
  would let the script read every spreadsheet you have.
- **Calendar write access**: the Apps Script project in your Google account holding `deploy/calendar_script/Code.gs`,
  deployed as a web app (Execute as: Me, access: Anyone), with the Script Property `CALENDAR_TOKEN` equal to
  `CALENDAR_WRITE_TOKEN` in `data/.env`, and its `/exec` URL in `CALENDAR_WRITE_URL`. Steps at the top of `Code.gs`.
- The `claude` login on this Mac (the subscription the `agent_sdk` engine bills); `ANTHROPIC_API_KEY` in
  `data/.env` is the fallback when the window is used up.
- **Grubhub blocks signing in from an automated browser** (2026-11-19): signed out, menus and the bag work, but
  the sign-in form leads to `grubhub.com/captcha/verify` ("Access to this page has been blocked", Press & Hold,
  "Please try again" for a real person too), and "Continue with Google" ends at Google's "This browser or app may
  not be secure". So the agent only chooses and Eli orders. Checking that a site lets the browser *sign in* comes
  before building. Grubhub's restaurant pages loaded on the Mac but came up empty ("Prepare your taste buds...")
  in the server's own Chromium, which is why the browsing stays on the Mac.
- **`Text(someString)` in SwiftUI does not make URLs tappable**; only a literal or an `AttributedString` with link
  attributes does (`Linkified` in `ChatView.swift`). Grubhub's app claims `/restaurant/*` links
  (its `apple-app-site-association`); whether a dish-level link lands on the dish is not known.
- **Uber Eats refuses automated browsers** (Cloudflare "Performing security verification", HTTP 403, from the Mac
  with the real Chrome, 2026-11-19); Grubhub does not. If Grubhub starts to, `food_view` returns `blocked` and the
  agent stops and says so. Do not add anything that disguises the browser.
- **The food browser's sign-in must be made in the window Playwright opens** (`bridge/food_signin.py`), not in a
  Chrome started by hand on the same profile: Playwright starts Chrome with a mock keychain, and cookies written
  under the real one cannot be read back.
- **Grubhub pages**: a dish opens as a dialog (the tools then show only the dialog); its add button reads "Make 1
  required selection" until the required options are chosen; address boxes only suggest while keys arrive, so
  `food_type` types key by key.
- **A tool result over about 25,000 tokens never reaches the model**: the CLI saves it to a file and returns an
  error instead. `sleep_summary` did this from 2026-11-19 (Sleep Bandit's answer is 100,000 characters whatever
  `days` is, nearly all per-dose tables), so the connector trims it to 36,000 characters. The registry cuts any
  result at 60,000 characters, which number-heavy JSON turns into more than 25,000 tokens, so the CLI is also
  started with `MAX_MCP_OUTPUT_TOKENS=40000` (not yet seen working on a real oversized result).
- The tracker's agent API (`/api/agent`) and Sleep Bandit's agent routes are additions in those repos
  (`project-tracker-and-organizer` has it in `tracker/agent_api.py`, see its README "Agent API"; `sleep-bandit`
  gained `GET /api/agent/summary`, `GET /api/oura/collection` and `oura.fetch_collection`). A running copy
  started before they were added needs a restart (quit and reopen the Task Tracker app;
  `launchctl kickstart -k gui/$(id -u)/com.elityre.sleep-bandit`).
- Nightblock (00:00–06:00) on the Mac: localhost, the Anthropic API, the whole Tailscale network (so the Atlas
  app, `ssh atlas-server` and the bridge; since 2026-11-18) and Toggl (since 2026-11-19) pass through. Otter,
  Apple push and Twilio hosts are still blocked from the Mac, which only matters if the agent runs there
  (`back-to-mac`): add them with `nightblock config-add direct_hosts` (the command is in `NEXT_STEPS.md`).
  `script.google.com` (exercise export) is open to Chrome only, not to scripts on the Mac.
- macOS notifications for the Mac app need a stable code signature (`build.sh` signs with the Apple Development
  certificate from Xcode → Settings → Accounts); System Settings → Notifications → Atlas → Allow; add the app to
  Login Items.
- Deferred by Eli (2026-11-06): the public HTTPS URL (Tailscale Funnel), Twilio calls, Apple Developer Program /
  iPhone push. Steps are in `NEXT_STEPS.md`.
- The LLM inbox's own settings (`NTFY_TOPIC`, email) are in `~/Library/Application Support/LLMInbox/config.env`.

## Gotchas learned

- **The tracker tools say "priority", 1 highest; the tracker stores `importance`, 5 highest** (2026-11-20, Eli: "it's
  not an importance, it's a priority"). `ea/tools/tracker_tools.py` translates both ways (`priority = 6 - importance`)
  in the wrapper around every tracker tool, so the agent sees the word and the numbers of the tracker's page (`#p1`
  is the top). Before this the tools took `importance` with "1 = low", and the outline printed the stored number as
  `pN`, so a `#p1` task read as "p5". Text the tracker itself writes (its change notes) still says "importance N".

- **A screenshot can be sent as a path on the Mac** (2026-11-18): a message containing `/Users/.../x.png` (or
  `~/Desktop/...`; png, jpeg, gif, webp) has that file fetched and attached like an image from the app
  (`ImageStore.from_paths`, `GET /mac/image` on the bridge). Only Downloads and Desktop are served. A copied
  screenshot path has a plain space where the file name has U+202F before AM/PM; `read_mac_image` matches it anyway.
  When the file cannot be fetched (Mac asleep, another folder) the message says so to the model in a bracketed line.
- **Never give a connector an attribute with a method's name.** `self.notes = []` on `ProjectTracker` hid
  `notes()` and `tracker_notes` failed with "'list' object is not callable" (2026-11-18).
- **The tracker outbox is only changed under its lock, from a fresh read.** `replay()` once wrote back the list it
  read before trying the Mac (which takes as long as the connect timeout when the Mac is asleep) and wiped a change
  queued in between: of two tasks created together, one arrived (2026-11-18).
- **iPhone message field**: SwiftUI's vertical `TextField` writes the sent text back into the binding after
  `onSubmit`, sometimes a moment later and with the last word autocorrected, so it is not equal to what was sent.
  `ChatStore.send` keeps the field clear for a third of a second (`clearingSent`).

- **A document with a space in its name would not open in the app (HTTP 400 "document names are...").** The app
  percent-encoded the name and then built the URL with `appendingPathComponent`, which encodes again: the server
  got `Locked-in%20day`, literally, and refused it. Only `workspace` had ever been opened, so it went unnoticed
  until the agent made "Locked-in day - shareable" (2026-11-16). In the server log the sign is `%2520` in the path.
- **The document list sometimes failed with a 500** (`sqlite3.InterfaceError: bad parameter or other API misuse`):
  all documents share one SQLite connection and the web server reads it from several threads. Every use now holds
  one lock (`ea/workspace.py`, `_DB`).
- **A message typed while a background turn ran vanished from the chat and looked unsent.** The agent got it (it is
  handed to the running turn), but it was stored under that turn's channel, `scheduler`, which the chat history
  hides; and the server said "done" before it was stored at all, so the app's history reload dropped it from the
  screen. Now a mid-turn message is stored under its own channel and "done" waits until it is stored (2026-11-16).
- **Calendar writes go through an Apps Script, reads through the iCal feed, and the feed lags.** An event the agent
  just created can take minutes to show in `calendar_events`; the create result (with its `event_id`) is the proof.
  The script uses the Calendar API (the "Calendar" advanced service), not `CalendarApp`: `CalendarApp` only runs
  with full calendar access, and the narrow `calendar.events.owned` scope is the point (changing the manifest's
  scopes means authorising again).
  After editing `deploy/calendar_script/Code.gs`, paste it into the script project again and deploy a **new
  version** (Deploy -> Manage deployments -> Edit), or the old code keeps running. An answer that is an HTML page
  instead of JSON means the deployment is not "Execute as: Me, access: Anyone" or the URL is not the `/exec` one.
- **The Mac app in /Applications went stale while a newer one ran.** `build.sh` used to relaunch the app from
  `apps/Atlas/build/`, so the login item in /Applications stayed at the 12 Nov build for three days and would have
  come back at the next login. `build.sh` now installs over `/Applications/Atlas.app` and launches that copy
  (2026-11-15). It also signs a copy in a temp folder, not in `build/`: under `~/Documents` the file provider
  puts Finder attributes back on the bundle within seconds and `codesign` refuses it ("detritus not allowed").
- **A phone that leaves the app right after sending resends the message.** iOS cuts the streaming request when
  the app goes to the background; with no reply text yet the app cannot tell "never arrived" from "arrived,
  reply lost", queues the message and sends it again on return. The server ran it twice and the agent answered
  twice (2026-11-15). Every send from the apps now carries `message_id` (the app's own id for that message,
  the same on a resend); `ea/channels/web.py` keeps the last 500 in memory and answers a resend with the first
  run's reply (log: `message ... was already received; not running it again`). A server restart between the
  two sends forgets the ids, so that one case can still run twice.

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
- The exercise export (`script.google.com`) cannot be reached by a script on the Mac through nightblock
  overnight. The Toggl desktop app's hosts are allowed directly in the firewall since 2026-11-19.
- Otter ingestion marks a recording seen before the agent reads it, so a crash mid-turn cannot loop on it; the
  first run marks all existing recordings as seen (history is not news).
- The shared `llm_contact.py` is imported from `~/Documents/agent-coding/shared` (`EA_SHARED_DIR` overrides); a
  Docker image without it fails at import on purpose rather than running without the preamble.
- `note_to_eli` is recorded by `contact.check()`, not by the tool's handler: the handler has only the message,
  while `check` has the system prompt, history and channel. Recording in both would file every note twice. The
  server's copy of `shared/` is taken at `deploy` time, and `ea/contact.py` needs `NOTE_TOOL` from it, so code and
  `shared/` always go up together (`atlas-server.sh deploy` does that). A note left during the fast-lane
  keep-alive ping is recorded, but the model is told no tools ran (no tool runs for a keep-alive).
- While the night block is on, nightblock's proxy answers 502 "could not reach" for a local service that is down
  (instead of a refused connection), so "the tracker is not running" has three spellings; `tracker_tools.guard`
  knows all three. `tests/test_tracker_open.py` fails at night without that.
- Server deployment gotchas (root and the Claude CLI, Docker and ufw, HOME on the data volume, session folder
  names, the night block and Tailscale) are in `deploy/server/README.md`.
