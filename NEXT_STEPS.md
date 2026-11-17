# Next steps: getting the executive assistant running

Written 2026-11-04; updated 2026-11-05 late evening (stage 2 built, see below). Stage 1 is done: the key is in `data/.env`, `python -m ea check --llm`
passed, and the web chat at http://localhost:8800 answers (server started by hand with `python -m ea serve`;
it stops when this terminal session ends). Every other channel still needs credentials only Eli can supply. In the order that gets something usable soonest:

## Where things stand (2026-11-06 21:15)

Running on this Mac under launchd, with the Mac app in /Applications as a login item. Live: Claude Agent SDK
on the subscription with API fallback; tracker, Focus Monitor, Sleep Bandit/Oura, Toggl, exercise log, Google
Calendar feeds, Otter (1,324 recordings; new ones read as they arrive), Roam (daily GitHub backup), the site's
source docs and private docs (read live; the agent is told of each settled edit with a diff), headless browsing,
the shared workspace/documents with Literal's editor and Mermaid diagrams, the thinking pane, Mac notifications.

**Deferred by Eli (2026-11-06, "these can all wait"):** the public HTTPS URL (Tailscale Funnel), Twilio calls.

**iPhone app (2026-11-11, Eli: "Let's install it on my iPhone"):** the Mac already has a *paid* Apple Developer
team (DW72928GK6, the one the Flutter apps deploy with), so no enrollment wait. `apps/Atlas/build-ios.sh --no-push`
builds the iPhone app signed with that team, and it is INSTALLED on the iPhone 16e (21:05, over the cable).
Offline support built the same evening: history cached in the app, messages typed without a connection queued
and sent when it returns. Still to do: (a) Eli opens Atlas on the phone and scans the setup QR
(`atlas://setup?url=…&token=…`; regenerate with any QR tool from `EA_WEB_TOKEN` and `http://Elis-MacBook-Pro-2.local:8800`);
(b) ~~agree to the PLA~~ done 21:20; `build-ios.sh` then created the push App ID and the build with
`aps-environment` is installed on the phone (verified with codesign). APNs key created 21:25 (Sandbox & Production, team
scoped), installed in `data/` and `data/.env`; `ea check` says `push: configured (com.elityre.atlas)`. PUSH WORKS: at 21:45 the phone,
on Tailscale from a coworking Wi-Fi, registered (sandbox, read from the embedded profile) and
`python -m ea push-test` got `200 ok` from Apple. Nightblock still needs `api.push.apple.com` / `api.sandbox.push.apple.com` allowed (step 3)
for overnight pushes; (c) Tailscale: the Mac is on it since 21:50 (App Store app,
`elis-macbook-pro.tail5a9488.ts.net`, server reachable on it); the phone needs the Tailscale app signed in to the
same account, then the setup QR with the tailnet URL (Eli was on a coworking Wi-Fi that isolates devices, so the
`.local` route never connected). No Funnel: nothing is public; push does not need it. Speech-to-text one-button entry: not built; the recipe
is `ios/SHORTCUT.md` (Shortcut: Dictate Text → POST /api/message → Speak), ~3 minutes by hand in Shortcuts.
Also 2026-11-11 late: Toggl is now read from the Focus Monitor's synced table (`sources.toggl_source`, default
`focus_monitor`; `"toggl"` goes direct again), because the agent's own polling kept hitting the hourly Toggl quota
the two apps share; the Focus Monitor got two read endpoints for it and was restarted. Messages sent while the agent is mid-turn now reach it right away on the SDK engine too
(streaming input to the CLI; before, they waited for the turn to end and arrived in a batch, which Eli saw from
the phone). Every proactive turn now starts with `config.WAKEUP_RULE` (read checklist + Toggl first), at the
agent's own request.

**Fast mode (2026-11-14, Eli: "fast mode when and only when it's talking synchronously with me").** Built:
chat and voice turns ask for fast mode, every background turn (checks, wakeups, heartbeat, ingestion, workers)
runs at standard speed; `llm.fast_when_chatting` in config.toml. Verified live: the CLI turns it on/off per turn,
and the API-key path returns `speed: fast`. **Needs Eli:** on the subscription fast mode is paid from extra-usage
credits and the account's monthly spend limit is used up ($50.58 of $50 on 2026-11-14, with $199 of credits
unspent; the CLI says "Fast mode disabled · usage credit limit reached"), so chat turns are still served at
standard speed until the limit is raised at claude.ai → Settings → Usage → Monthly spend limit → Manage. Nothing to change
here afterwards; the log line `fast mode not served this turn` stops appearing. *Limit raised by Eli the same
afternoon; a probe turn then came back `speed: fast`.* Usage is tracked by speed from 2026-11-14 15:3x:
`python -m ea usage` (tokens and estimated cost, fast vs standard, and how much was actually charged). Worth
watching there: each switch between a chat turn and a background turn rewrites the prompt cache at the new speed.

**Two threads (2026-11-15).** Replaying 6 to 14 Nov priced fast mode on one session at $44 to $72 a day, nearly
all of it cache rewrites at speed switches and after idle hours. Now: chat runs on its own fast session (a fork
of the main one, full raw history), background work on the standard one, each caught up on the other at the
start of its turn (text in full, tool calls and results shortened), and the chat session's cache is re-read
every 55 minutes so it does not expire between chats (README, "Two threads"). Estimate from the same replay:
about $24 a day. **To watch:** `python -m ea usage` after a few days; the first chat after the restart pays one
full rewrite (about $3 at 200k tokens) to create the fork. `llm.fast_own_thread = false` goes back to one thread.
The agent now watches this itself: a `model_usage` tool, the memory `model-costs`, a check-in to Eli every 7
days and an alert the day charged spend passes $150 (README, "Usage by speed"). After the move to a server the
cold starts from the Mac sleeping go away. Still to confirm: that credits are debited at exactly list prices
(compare the credit balance on claude.ai with `python -m ea usage`).

## Stage 1: talking to it on this Mac (about 15 minutes, no cloud)

1. Anthropic API key into `data/.env` (copy `.env.example` there first). This is the one hard blocker; no model
   call has been made yet.
   *2026-11-04 (Claude): `data/.env` now exists with every key blank except a generated `EA_WEB_TOKEN`. Paste the
   Anthropic key after `ANTHROPIC_API_KEY=` on line 2 and restart `python -m ea serve`. The web chat is at
   http://localhost:8800 and wants the `EA_WEB_TOKEN` value in its token box once. Decided the same day: the end
   goal is the cloud deployment (stage 4), so the agent keeps running while the laptop is closed.*
2. Toggl API token (Toggl → Profile → API token) and the exercise export URL (the same `/exec` URL that sits in
   the tracker's Settings page) into `.env`. Optional but they are two of the five data sources.
3. Calendar: paste the Google Calendar secret iCal address into `CALENDAR_ICS_URLS`, or skip it and the agent
   uses the tracker's single-day view.
   *2026-11-04 (Claude): `TOGGL_API_TOKEN` is set and `ea check` reads Toggl (34 entries). Exercise URL still blank.*
   *Nightblock (00:00-06:00): everything on localhost and the Anthropic API already pass through. Eli asked
   (2026-11-04 late evening) that the agent AND the Toggl Track desktop app (native Swift, may skip the proxy) be
   exempt. STILL NOT DONE as of 2026-11-05 00:20: it needs the admin password, `!` has no terminal for sudo,
   and Claude Code's permission layer refused the GUI password dialog. Run this in Terminal.app:*
   ```
   for h in api.track.toggl.com track.toggl.com accounts.toggl.com desktop.track.toggl.com sync.toggl.com focus.toggl.com otter.ai api.push.apple.com api.sandbox.push.apple.com api.twilio.com; do sudo nightblock config-add direct_hosts $h; done; sudo nightblock allow '*.toggl.com'; sudo nightblock allow otter.ai; nightblock check desktop.track.toggl.com
   ```
   *`script.google.com` (exercise export) can only be opened via the proxy, not for Python, so that source will
   stay blocked overnight.*
5a. Otter.ai transcripts (added 2026-11-05 at Eli's request: "read access to all my otter transcripts").
   *Done by Claude: `ea/connectors/otter.py` + tools `otter_recent`, `otter_search`, `otter_transcript`,
   `otter_status`; `python -m ea otter` builds `data/otter.db`. The Logseq archive (the Roam extension's
   three "Otter auto-import…" pages in `~/Documents/main-logseq-graph`) is indexed: 429 recordings,
   2024-01-01 to 2026-04-11, FTS5-searchable, folders walking journal 47 / audio notes 10 / unfiled 372.*
   *Still needed from Eli for the live part (recordings after April 2026 and anything new): put the Otter
   login in `data/.env` as `OTTER_EMAIL` / `OTTER_PASSWORD` (a Google-sign-in account needs an Otter password
   set on otter.ai first), then `python -m ea otter --all` once for the full history and restart `serve`.
   Otter's unofficial API has not yet been exercised against the real account (no credentials, and otter.ai
   is blocked by nightblock tonight), so the first `--all` run is the real test. The server also needs
   otter.ai in nightblock's direct_hosts (in the command above).*
4. Commit the uncommitted agent routes in `project-tracker-and-organizer` and `sleep-bandit`, or at least don't
   reset those repos. To make the tracker require a token, set one in its Settings → Assistant agent and mirror
   it as `AGENT_API_TOKEN`.
   *2026-11-04 (Claude): tracker read AND write access verified live through all 20 `tracker_*` tools (outline,
   search, create/update/complete task, notes, schedule context). Two tracker fixes made the same evening, still
   uncommitted there: `PATCH /api/tasks` now parses deadline words ("tomorrow", "fri") instead of storing them raw,
   and deleting a task removes the agent's notes on it. The tracker must be running (`dist/Task Tracker.app`) for
   any of this to work; no token is required at present.*
5. ~~Run `python -m ea check --llm`, then `python -m ea chat`.~~ Done 2026-11-04 21:29; the first real turn was the
   evening check, which flagged task 76 "Write up what we need" (due 2026-11-05, nothing planned). Still to do:
   write the real habit documents into `instructions/habits/`; the agent is currently measuring against the
   EXAMPLE template's 90-minute focus target and said so.

Claude can do step 5 and verify the whole loop end to end the moment the key is in place.

5b. Proactive behaviour (added 2026-11-05 at Eli's request: message proactively, set its own timers/triggers,
   wake up stochastically about hourly).
   *Done by Claude: `schedule_wakeup` tool (a prompt the agent runs a turn on at a time, optional repeat;
   `list_reminders` / `cancel_reminder` cover it), and a `[heartbeat]` section in `config.toml`, ON: random
   turns with exponential gaps (mean 60 min, clamped 20-180) between 08:00 and 23:00, next time persisted in
   `data/state.db`. `python -m ea run-check heartbeat` runs one by hand. Server restarted with it at 11:40; first heartbeat drawn for 12:03.*
   *Caveat: until the push channel exists (stage 2), everything the agent sends proactively lands only in the
   web chat's history at http://localhost:8800 (marked "sent by the agent at ..."); nothing reaches the phone.
   Stage 2 is what makes proactive messaging real. Also worth deciding: the heartbeat window and mean.*

## Stage 1c: Roam and Otter (added 2026-11-06 afternoon at Eli's request)

**Roam.** Drop the existing export in `data/roam/` (JSON from Export All is best; the Markdown .zip or .md files
also work). The agent then has `roam_search`, `roam_page`, `roam_recent`, `roam_backlinks`, `roam_pages`,
`roam_status`; the index (`data/roam.db`) rebuilds itself whenever a file in that folder changes;
`python -m ea roam` does it by hand. *Eli said this should not be modelled on the Otter approach; what exists
is only the reader over the folder, to be reshaped once he says how he wants it.*
- *Done 2026-11-06 20:50: daily updates from the GitHub backup. `sources.roam_backup_git =
  git@github.com:Etyre/roam-backup-JSON-only.git` (one file, `eli.json`, committed by a GitHub Action daily at
  11:13 UTC); cloned shallowly to `data/roam/backup`, the scheduler fetches + hard-resets every `roam_sync_hours`
  (6) and re-indexes on change. When the backup checkout exists the connector reads only it, so the zip Eli
  dropped on 11-06 is now ignored and can be deleted. Note the backup lags a day (the dropped zip was newer).*

**Otter, "all my recordings are shared with the agent and it reads them all".** Built 2026-11-06: ingestion.
Every `otter_sync_minutes` (15) the scheduler pulls new recordings; each unseen one is handed to the agent in
its own turn (`[otter recording: ...]` with the transcript, up to `otter_ingest_max_chars`); the agent files
tasks/decisions/memories per the new "My recordings" section of `STANDING_INSTRUCTIONS.md` and messages only
if something is time-sensitive. Existing history is marked already-seen on the first run, so nothing floods.
`[sources] otter_ingest = false` turns it off. Tests cover it with a fake Otter.
- *2026-11-06 20:45: login in place; Otter's unofficial API works on the real account (first pull: 9 of the
  newest 10 in 10 s). Found and fixed: listing page 2+ needs a `modified_after` cursor. Full-history pull run
  the same evening; ingestion of new recordings starts with the server restart after it.*
- *Also done tonight: exercise URL + calendar feeds imported from the tracker's config (18 events / 18 sessions
  read live); server under launchd (`com.elityre.executive-assistant`); app in /Applications + login item.*

**elityre.com source docs (Eli, 2026-11-06 15:15: "the agent should have an up to date version of all the
source docs").** Done, live rather than synced: `[[document_roots]]` in config.toml points at
`~/Documents/Personal website/elityre.com/source_docs`; `list_documents` shows them as `elityre.com/<file>.md`
with last-modified dates and `read_document` returns the current file from disk, so every save is visible on the
next read with nothing to copy. The prompt lists the root with its file count and last change. 33 files. Caveat
for the cloud deployment (stage 4): the folder is on the Mac, so the bridge would have to serve it.

## Stage 2: dedicated iPhone + Mac apps for text (DECIDED 2026-11-05, replaces Telegram)

Eli decided 2026-11-05: no Telegram. The agent's text notifications go to a dedicated iPhone app and a
dedicated Mac app (one SwiftUI multiplatform Xcode project, `apps/Atlas/`). Phone calls stay on Twilio
(stage 3). The Telegram channel code stays in the repo, optional and unconfigured.

*2026-11-05 evening (Claude), "build everything": ALL the code is written and tested; what remains is the Apple
account work only Eli can do.*
- *Server: `ea/channels/push.py` (APNs over HTTP/2, ES256 provider token, per-device sandbox/production,
  dead tokens forgotten), `ea/channels/events.py` (live feed), endpoints `POST/GET/DELETE /api/devices`,
  `GET /api/events`, `POST /api/push/test`, `python -m ea push-test`. Every proactive message (send_message,
  reminders, heartbeat) now fans out to push + live feed (+ Telegram if ever configured). Replies are also
  pushed, collapsed, to the device that asked. 60 tests pass (9 new, with a fake Apple). Server restarted on
  the new code at 23:57, now detached (`nohup`, log in `data/logs/serve.log`), so it survives this terminal.*
- *Apps: `apps/Atlas/` builds clean for macOS and the iPhone 17 Pro simulator (xcodegen project; run
  `xcodegen generate` after editing `project.yml`). Chat with streaming replies and "looking at…" status,
  live sync across devices, day stamps, settings (URL, token in Keychain, device name, push env, test button),
  menu-bar window on the Mac, generated app icon. An unsigned debug build of the Mac app was launched once to
  confirm it starts; it is the window showing the Settings sheet. Full guide: `apps/Atlas/README.md`.*
- *`instructions/STANDING_INSTRUCTIONS.md` got a "Reaching me" section: notifications by default, a call only
  for time-critical things, never in the night block.*

*2026-11-06 00:30 (Claude), after Eli chose to focus on the Mac app / web page first: notifications WORK on the
Mac today, without Apple. The app raises its own system notifications from the server's live feed whenever a
message from the agent arrives while it is not in front (and the web page does the same with browser
notifications once "enable notifications" is clicked). New: `POST /api/notify` and `python -m ea notify "text"`
send a message exactly as the agent would. Tested live: Release build launched from
`apps/Atlas/build/Build/Products/Release/Atlas.app`, configured by `defaults write com.elityre.atlas baseURL/token`
(token auto-moved into the Keychain), history loaded, live feed connected, two notifies delivered to it (the
second with Finder in front, so it should have shown a banner; Eli to confirm he clicked Allow on the
notification permission dialog). Server restarted 00:2x on the new code, still a `nohup` process.*

*Blocked for Claude (permission classifier refused "persistence" actions), so Eli runs these in Terminal:*
```
cd ~/Documents/agent-coding/executive-assistant-agent
bash deploy/macmini/install.sh                       # server under launchd, survives reboot/logout (kill the nohup `ea serve` first: pkill -f "ea serve")
apps/Atlas/install-auto-deploy.sh                    # the Mac app: every commit is built and installed in /Applications when the agent is idle (2026-11-16); then System Settings -> General -> Login Items -> + Atlas
```
*Then System Settings → Notifications → Atlas: allow, style Alerts or Banners. Without a paid Apple account the
Mac app is ad-hoc signed: fine for running locally; Apple push on it stays off (not needed while it runs).*

6a. **Mac app, now**: the above. Rebuild after code changes with `cd apps/Atlas && xcodebuild -scheme Atlas
   -configuration Release -destination 'platform=macOS' -derivedDataPath build CODE_SIGNING_ALLOWED=NO build`.
*2026-11-06 00:30 (Claude). Eli's rule: "Don't send a message over my notifications if I'm in the chat with the
agent. Only make it show up in notifications to get my attention." Done server-side so it holds across devices:
apps/pages report `POST /api/presence` while their chat is in front (and typing counts for 3 min); while anyone
is in the chat the agent's messages carry `quiet: true`, every client shows them in the thread without a banner,
and no phone push goes out. Verified live: Atlas in front → notify says quiet; Atlas behind another app → banner.
Reply pushes to a phone that locked after asking are still sent (only in-front clients suppress those).*

*Canonical laptop chat (Eli asked 2026-11-06): the Mac app. The web page stays as a fallback and gets the
same live feed + quiet logic, but no further work goes into it.*

*2026-11-06 01:00 (Claude). Chain-of-thought pane, at Eli's request: the Mac app now has the chat on the left
and an optional "Thinking" pane on the right (toolbar brain button, View menu, ⌘⇧T; remembered). It shows every
turn, including the agent's own checks and heartbeats: what started it, the model's reasoning summary, each tool
call with its input, what came back, and what it decided. Server: `GET /api/activity` (stored, built from the
conversation blocks) and live `{"type":"activity"}` events on `/api/events` from `Agent.activity`. Found and
fixed on the way: Opus 5.5 returns thinking blocks with empty text unless asked, so the provider now sends
`thinking.display = "summarized"` (`llm.thinking_display` in config.toml). Verified live: summaries stream into
the pane during a real turn. 64 tests pass. Server restarted and the app rebuilt/relaunched with it.*

*2026-11-06 15:05 (Claude), afternoon round of fixes and features on the Mac app and server:*
- *"Nothing changing in the chain of thought": two real bugs. (1) `pkill` of the server left the OLD process
  alive (uvicorn waits forever for open event streams), so the app stayed attached to a dead bus that still
  pinged it; fixed with `timeout_graceful_shutdown=3` and a `bye` event on shutdown. (2) The app's URLSession had
  `waitsForConnectivity` on, so a refused connection during the restart window hung forever; off now, plus a
  60 s no-ping watchdog and a reload of history/activity on every reconnect. Verified: after a server restart the
  app is back on the live feed within 3 s. App-side diagnostics now go to `~/Library/Logs/Atlas/atlas.log`.*
- *Thinking pane: sticky autoscroll (follows new rows only while at the bottom; a "Latest" button appears when
  scrolled up) and the scroll position is restored after an app restart. Chat pane: always opens and stays at
  the latest message (re-settles after the lazy rows lay out). Shift+Return inserts a newline in the composer.
  Thinking rows show an expand chevron; the text is Anthropic's reasoning SUMMARY (the raw chain of thought is
  never returned by the API).*
- *Keychain password prompts on every launch: each rebuild is ad-hoc signed with a new identity, so the Keychain
  no longer trusted the app. On the Mac the token now lives in `~/Library/Application Support/Atlas/token`
  (mode 600; the same token is in data/.env anyway); iOS keeps the Keychain. The old Keychain item was removed.*
- *Integrations record, per Eli: `integrations_status` tool (+ `ea/integrations.py` registry: purpose, state,
  what is missing, how to get it, who acts) and an "Your integrations" section in STANDING_INSTRUCTIONS.md making
  the agent keep a memory entry `integrations` and tracker tasks under Atlas / Questions for Eli. `POST /api/run`
  and `python -m ea run "..."` hand the agent a background task (shows in the thinking pane, not in the chat).
  Still to do: run `python -m ea run "Build your integrations record now"` once Eli is not mid-conversation.*

*2026-11-06 19:10 (Claude). Mac notifications had NEVER actually been allowed: the ad-hoc signature changed on
every rebuild, so macOS refused permission (`UNErrorDomain error 1`, the status Eli saw in Settings). Fixed by
signing with the free "Apple Development" certificate already in the keychain; `apps/Atlas/build.sh` now does
build + sign + relaunch. Verified: permission granted, a real notification posted with the app in the background.
Also: single-instance guard (a second copy quits itself), unsent draft survives restarts. Settings sheet quirk:
the token field renders blank even though the token is loaded (the app is connected); cosmetic, not fixed.*

*2026-11-06 19:40 (Claude). Shared workspace, at Eli's request: a third pane, a Markdown whiteboard both edit,
separate from the chat. Server: `data/workspace.md` (also openable in Literal), every version kept in state.db
(`workspace_versions`, last 300), content-hash versions so outside edits count too; `GET/PUT /api/workspace`
(PUT with base_version; 409 + current text on conflict), `GET /api/workspace/versions/<v>`; live
`{"type":"workspace"}` events. Agent tools: workspace_read / workspace_edit (one exact passage) /
workspace_append / workspace_write (whole page, versioned) / workspace_history; the prompt names it. App: pane
toggled with the toolbar button or ⌘⇧W (off by default), autosaves 0.8 s after typing stops, reloads live when
the agent edits, and on a clash shows a banner (Load theirs / Keep mine), never silently. Window width is now
chat × visible panes. Verified end to end: the agent appended a section via its tool and the app received the
event. Chat pane also stays anchored at the bottom when the window is resized (Eli, 19:20).*

*2026-11-06 19:35 (Claude). Engine switched to the Claude Agent SDK on Eli's subscription, with the API as
fallback ("run on my subscription unless I'm close to running out"). `llm.provider = "agent_sdk"` in config.toml
(`"anthropic"` puts it back on the API key; restart either way). `ea/llm/agent_sdk.py`: our ToolRegistry becomes an
in-process MCP server, Claude Code's own tools are off, the subprocess gets `ANTHROPIC_API_KEY=""` so the CLI uses
the `claude` login, history is an SDK session resumed per thread, and every assistant block / tool result is still
written to the conversation store (apps, thinking pane, transcripts unchanged). Rate-limit events from the CLI
drive the fallback: at 90% of a window (or rejected) turns go over the API until the reset, then the session
restarts with a recap. Verified live: a real turn ran in ~10 s, tools and thinking streamed, and the CLI
reported the subscription's seven-day window (so it is the subscription, not the key). `/health` → `engine`.
Also: `workspace_open` tool, and any agent edit brings the workspace pane up in the app.*

*2026-11-06 19:50 (Claude). Subscription-limit tracking (Eli: "can this track when I'm getting close?"): every
rate-limit report the Claude Code CLI sends during a turn is kept (`python -m ea usage` lists them; window, status,
utilization when the CLI includes it, reset time). From 75% of a window (`llm.subscription_warn_utilization`) or the
CLI's own warning state, the agent messages Eli once per window; at 90% / rejected it switches to the API key and
says so. The agent's prompt has an ENGINE line with the current reading; the app's Settings has an Engine
section; `/health` → `engine`; `integrations_status` has an "engine" row. Limit: the utilization percentage
is only as available as the CLI makes it (the first reading had status only); status warnings always come.*

*2026-11-06 20:00 (Claude). Workspace pane rebuilt around Eli's own Literal editor: the pane now embeds
Literal's CodeMirror bundle (`apps/Atlas/Atlas/Resources/literal/`, copied from `literal-markdown/out/renderer.js`
+ styles) in a web view with a bridge shim standing in for Electron, so the page is rendered Markdown with the
markup hidden and fully editable in place; ```mermaid fences are drawn as diagrams below the text (mermaid.js
bundled, offline). Font 19px (Eli: 2x was too big, halfway). Documents: the whiteboard plus any number of
named shared pages (`data/workspace/<name>.md`, per-document history); picker + New/Delete in the pane header;
server `GET/PUT/DELETE /api/docs/<name>`; agent tools document_list/read/write/edit/append/delete/open, and the
prompt tells it to draw with mermaid when a picture says it better. `workspace_remove` + "whiteboard, not a log"
guidance in prompt and STANDING_INSTRUCTIONS. Copy: right-click menus on messages and thinking rows, ⌘⇧C copies
today's conversation. To refresh the editor after changing Literal: `npm run build` there, copy `out/renderer.js`
and `src/styles.css` into the Resources folder, `./build.sh`.*

*2026-11-06 20:20 (Claude). Headless web browsing, at Eli's request: Playwright + headless Chromium
(`ea/connectors/browser.py`, tools web_search (DuckDuckGo), web_open (page as text, paged), web_more, web_links,
web_click (follow a link by its text), web_fill (type into a form field, optionally submit)). JavaScript runs;
nothing persists (no cookies/logins); the prompt forbids entering passwords or paying. `sources.web_browsing =
false` turns it off. Chromium is in ~/Library/Caches/ms-playwright (`python -m playwright install chromium`
after a fresh install; the integrations record has a row for it). Tested against a local page incl. a
JS-inserted element, link following and form fill. Also this evening: the chat composer is a TextEditor now
(the vertical TextField would not wrap at narrow widths and clipped text; Eli, 20:05).*

*2026-11-06 21:30 (Claude). Sending during the agent's turn (Eli's request): the app's send button is always
live. A message sent mid-turn goes to `Agent.submit()`: on the API engine the running turn folds it into its next
step (a text block after the tool results, prefixed so the model knows it arrived mid-work); on the SDK engine,
or when the model has already finished, it starts the next turn by itself. The agent now publishes every chat
message to the live feed itself (`Agent.on_message`), so replies to queued messages reach the app; the app
dedupes a streamed reply against the feed copy. Verified live: two messages queued behind a running turn were
combined and answered. Mid-turn copies are stored in the history without the prefix.*

*2026-11-07 10:30 (Claude). Parallel workers, at Eli's request: `delegate(name, task)` starts a background worker
(`ea/agent/workers.py`): an isolated turn on its own conversation thread (`worker:<id>`, hidden from the chat), same
tools and memory plus a worker addendum, its own headless browser (Playwright is per thread), no access to the
conversation. Up to `llm.max_workers` (3) at once; `workers_status`, `cancel_worker`. A result returns to the main
agent as a mid-turn message if a turn is running, else as a proactive "[worker result]" turn (never answered to the
user as chat). Verified live: two workers ran side by side and finished in 17 s. The app shows "Working in the
background: …" above the composer. Also fixed: thinking-pane rows overlapping (selectable Text needed fixedSize).*

*2026-11-07 10:45 (Claude). Images in the chat, at Eli's request: paste (⌘V) or drag-and-drop images or image
files anywhere on the chat; they show as thumbnails above the composer (removable) and go with the next message,
downscaled to 1568 px and re-encoded in the app. Server: `ea/images.py` stores them as files under `data/images/`
(the conversation holds a file reference, inflated to a base64 block only when sent to the model), `MessageIn.images`
on both message endpoints (max 6), `GET /api/images/<name>` serves them back for the history, and the live feed
carries image names. Both engines: the API path sends image blocks; the SDK path uses `ClaudeSDKClient` streaming
input for turns with images (also removed an early `break` that left the SDK generator running). Verified on the
subscription engine: the agent described a test icon and remembered it next turn. Bubbles show the images.*

*2026-11-07 23:25 (Claude). "Not clearly getting notifications": they were being posted (4 that day, logged),
but macOS banners fade in seconds. Now the app shows its own persistent notice (`NoticePanel.swift`: floating
panel top-right, stays until clicked; click opens/activates the chat; cleared when the chat comes to the front)
in addition to the system notification. Clicking a system notification opens the chat and recreates the window
if it was closed (URL scheme `atlas://open` + `handlesExternalEvents`). For persistent system notifications too,
Eli can set Atlas to "Alerts" in System Settings → Notifications (apps cannot set that). Verified with a window
snapshot of the panel.*

6. **Eli, Apple side**: ~~join the Apple Developer Program~~ already a paid member (team DW72928GK6, found
   2026-11-11). Agree to the current Program License Agreement at developer.apple.com (Xcode blocks on it), then
   `apps/Atlas/build-ios.sh` creates the App ID with Push Notifications itself. Still by hand: Keys → an APNs auth
   key (.p8; download once, note Key ID; Team ID is DW72928GK6). Put the .p8 in `data/` and in `data/.env`:
   `APNS_KEY_PATH=AuthKey_XXXXXXXXXX.p8`, `APNS_KEY_ID=…`, `APNS_TEAM_ID=…`. Restart serve.
   `python -m ea check` then says `push (iPhone/Mac apps): configured`.
6b. **Install on the phone**: `apps/Atlas/build-ios.sh` (team is now in `project.yml`; `--no-push` while the PLA
   is unsigned). Phone awake on the same Wi-Fi or on a cable. First launch: scan the setup QR or enter server URL +
   `EA_WEB_TOKEN` in Settings; with push on, status reads `registered (sandbox)` and *Send test notification*
   proves the whole path (`python -m ea push-test`).
6c. For the iPhone away from home the server URL must be the public one, so step 8 (Tailscale Funnel) also
   matters for the app. On the home Wi-Fi `http://<mac-name>.local:8800` works.
6d. Nightblock: `api.push.apple.com` and `api.sandbox.push.apple.com` are already in the command in step 3.
7. Install the server as a launchd service with `deploy/macmini/install.sh` so it survives reboots (the
   `nohup` process from tonight dies at the next reboot or logout). This laptop is then the "Mac mini"
   deployment until the real one exists.

## Stage 3: phone calls FROM the agent (about an hour, needs a public HTTPS URL)

The direction that matters (Eli, 2026-11-05): the agent calls Eli, not the reverse. `place_call` in
`ea/channels/twilio_voice.py` and the `call_user` tool in `assistant_tools.py` already do this: Twilio dials the
number, the iPhone rings as a normal call, the agent speaks its opening line and converses. Needs 8 + 10.

8. A public URL for the webhook endpoints. Easiest: install Tailscale on the Mac, enable Funnel in the admin
   console, run `tailscale funnel --bg 8800`. Put the resulting URL in `channels.public_base_url` and set
   `EA_WEB_TOKEN`. The dedicated apps (stage 2) use the same URL when away from the home network.
9. (Optional now that there is an app) Build the Shortcut per `ios/SHORTCUT.md` and bind it to the Action button.
10. Twilio: buy a number, set its voice webhook to `<public_base_url>/twilio/voice`, put the account SID, auth
    token, number and your own number in config and `.env`. Then add a rule to `STANDING_INSTRUCTIONS.md`
    about when a call is warranted versus a push notification (a ring is a much bigger interruption).
11. ~~Voice notes on Telegram~~ dropped with Telegram; the apps use on-device dictation.

## Stage 4: the agent on a server (Eli, 2026-11-17: "do all the work you can to set up my agent to run on a reputable server")

Built and tested on the Mac side on 2026-11-17; guide: `deploy/server/README.md`. Done: the server image
(`deploy/Dockerfile`), `deploy/server/atlas-server.sh` (provision, deploy, env, check, cutover, back-to-mac),
the rewritten Mac bridge (installed under launchd as `com.elityre.ea-bridge`, verified against the real tracker,
Focus Monitor and Sleep Bandit), the document copy and the LLM-inbox relay through it, `config.server.toml`.

**Done 2026-11-17: the agent runs on the server.** A DigitalOcean droplet (San Francisco, 2 CPUs, 4 GB, weekly
backups; ssh host `atlas-server` in `~/.ssh/config`), reached at `http://atlas.tail5a9488.ts.net:8800` over
Tailscale. AWS Lightsail was tried first and refused (the account's vCPU quota was 0). `provision`, `deploy`,
`env`, `check` and `cutover --yes` all ran as written; the subscription token answered from the server; the
saved sessions were copied; push is configured there. The Mac's launchd job is disabled; `back-to-mac --yes`
reverses it. The Mac app points at the server; the iPhone app (updated the same day) needs the URL set by hand.

**Open after the move:**
- The night block (00:00-06:00) refuses Tailscale addresses from the Mac, so the Mac app cannot reach the agent
  in those hours (the phone can). Whether it also stops the server reaching the bridge then is untested. Exempting
  it is Eli's decision.
- `back-to-mac` has not been run.

**Nothing the agent reads depends on the laptop being awake (2026-11-17, Eli: "everything works the same but
doesn't depend on my laptop being awake"; then: "I want my project tracker to run locally").**
- Toggl: read from Toggl directly on the server.
- Sleep Bandit and Oura: Sleep Bandit moved to the server (`http://atlas:8777`); daily backups come back to the Mac.
- Focus Monitor: stays on the Mac; the server keeps a copy of its answers (ten-minute refresh).
- Project Tracker: stays on the Mac and the agent uses it there when the Mac is awake. Asleep: reads from a
  read-only copy on the server (five-minute sync), changes queued and made on wake.
- Not yet seen with the Mac really asleep overnight (only simulated). After the first night, check
  `/var/log/sync-tracker-copy.log` on the server and ask Atlas about a task from the phone with the laptop shut.
- A good URL for Sleep Bandit: Eli asked; options given, his choice pending.

## Decisions to make rather than default

- The agent's name (currently Atlas).
- The three check times in `config.toml`.
- Whether the tracker's agent API should require a token.
- Whether to add a license to the public repo (https://github.com/Etyre/executive-assistant-agent); none yet.
- Whether to replace `user_name = "Eli"` and the `com.elityre.*` launchd labels with placeholders in the public repo.
