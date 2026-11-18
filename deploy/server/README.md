# Running the agent on a server

**Live since 2026-11-17** on a DigitalOcean droplet (ssh host `atlas-server`), at `http://atlas.tail5a9488.ts.net:8800`.

Built 2026-11-17 so the agent keeps answering, keeps its schedule and keeps reading new recordings while the
Mac is shut. Everything is driven from the Mac with one script, `deploy/server/atlas-server.sh`.

## How it works

```
 iPhone app ─┐                                   ┌── Anthropic, Otter, Google, Apple push (straight out)
 Mac app ────┼─ Tailscale ─▶ server: Docker `atlas` (python -m ea serve), state in /opt/atlas/data
             │                 │
             │                 └─ Tailscale ─▶ Mac: bridge :8799 ─▶ Project Tracker :8791, Focus Monitor :8790,
             │                                  (only while the Mac is awake)   Sleep Bandit :8777, document folders,
             └────────────────────────────────                                  the LLM inbox, "open the tracker"
```

- **The server** is any Ubuntu 24.04 machine. It runs one Docker container as a non-root user. Nothing listens
  on the internet except ssh: the firewall denies the rest, the container's port is bound to the machine itself,
  and `tailscale serve` hands the tailnet's port 8800 to it. The apps reach it at
  `http://atlas.<tailnet>.ts.net:8800` with the same token as before.
- **The model** runs on the Claude subscription, as on the Mac. The server has no browser to sign in with, so it
  uses a long-lived token made once on the Mac with `claude setup-token` (`CLAUDE_CODE_OAUTH_TOKEN`). The API key
  remains the fallback when the subscription window is nearly used up.
- **The Project Tracker stays on the Mac** (Eli's decision, 2026-11-17) and is reached through the bridge
  (`bridge/mac_bridge.py`, launchd agent `com.elityre.ea-bridge`) whenever the Mac is awake, for reads and
  writes. For when it is asleep the server keeps a **read-only copy** (container `tracker`, from the tracker
  project's `deploy/tailnet`): `sync-tracker-copy.sh` (cron, every five minutes) asks the bridge for
  `/tracker-copy` and swaps the snapshot in when it changed. With the Mac away, tracker reads come from the copy
  with a note saying when it was synced, and changes go to `/opt/atlas/data/tracker_outbox.jsonl`, which the
  scheduler replays on the Mac once it answers again (`ea/connectors/project_tracker.py`). A replayed change the
  tracker refuses is reported in the next tracker tool result.
- **Sleep Bandit runs on the server** (container `bandit`, from its project's `deploy/tailnet`), with Oura syncing
  by itself; the agent reaches it at `http://bandit:8777` (`sources.direct`), Eli at `http://atlas:8777`.
- **The Focus Monitor stays on the Mac** (it watches the Mac) and is read through the bridge.
- **Time zone.** The tracker copy and Sleep Bandit work in local time. `follow-mac-timezone.sh` (cron, every ten
  minutes) reads the Mac's zone from the bridge's `/health` and recreates a container whose zone differs.
- **Document folders** (`[[document_roots]]`) are copied from the Mac to `/opt/atlas/data/docs/<name>` through
  the bridge every two minutes (`ea/docs_mirror.py`), with their exact modified times, so the "document edited"
  notices behave as before.
- **Focus Monitor answers are kept.** Every ten minutes the server copies the Focus Monitor's metrics (120 days)
  and day detail (today, yesterday, and the fortnight before when missing) into `/opt/atlas/data/focus_cache.json`
  (`ea/connectors/focus_monitor.py`). With the Mac asleep the focus tools answer from that copy, with a note saying
  when it was taken; the monitor records nothing while the Mac sleeps, so the copy is complete.
- **Toggl** is read straight from Toggl's API on the server (`toggl_source = "toggl"` in `config.server.toml`).
- **The LLM inbox.** A message from the model to Eli is recorded on the server and Eli is notified from there
  (ntfy / email, the same settings as on the Mac). The record is then copied into the Mac's inbox through the
  bridge (`ea/inbox_relay.py`), at once if the Mac is awake and otherwise when it wakes, so `llm-inbox` and the
  page on :8798 show it with its full context.
- **The agent's conversation** moves with it: `state.db`, memory, transcripts, and the Claude CLI's session files
  (`~/.claude/projects/<data folder>` on the Mac, `/opt/atlas/data/home/.claude/projects/-data` on the server).
- **Only one copy runs.** `cutover` stops the Mac's agent (and disables its launchd job) before the server's
  starts; `back-to-mac` does the reverse. Two copies would both send check-ins and both read new recordings.
- **Code** reaches the server only as a commit: `deploy` exports HEAD, adds `agent-coding/shared`, and builds.
  The server never needs GitHub access for the code.

`config.toml` is the same file on both machines; `config.server.toml` (untracked, from
`config.server.example.toml`) is laid over it on the server and holds the few differences.

## Files

| File | Purpose |
|---|---|
| `atlas-server.sh` | `provision`, `deploy`, `env`, `check`, `cutover`, `back-to-mac`, `status`, `logs`, `ssh` |
| `sync-tracker-copy.sh`, `follow-mac-timezone.sh` | Run by cron on the server (installed by `provision`): refresh the tracker's read-only copy; keep the moved tools in the Mac's time zone |
| `config.server.example.toml` | Template for `config.server.toml`: the bridge address, where the document copies are |
| `../Dockerfile`, `../docker-compose.yml` | The image and how it is run |
| `../../bridge/` | The Mac side: `mac_bridge.py`, its launchd plist, `install_bridge.sh` |

## Runtime locations

On the server: code in `/opt/atlas/app` (`VERSION` names the commit), state in `/opt/atlas/data` (never rebuilt),
secrets in `/opt/atlas/data/.env` (mode 600), config in `/opt/atlas/config.toml` and `config.server.toml`, the
agent's home folder (sessions, LLM inbox, the Roam deploy key) in `/opt/atlas/data/home`. Logs: `docker logs atlas`
(20 MB × 5 kept).

On the Mac: server-only secrets in `data/server.env`; the bridge token in `data/.env` (`EA_BRIDGE_TOKEN`); the
bridge's log in `~/Library/Logs/ea-bridge.log`.

## Setting it up

1. **Rent the server** (Eli): Ubuntu 24.04, 2 CPUs, 4 GB of memory, 40 GB of disk or more, in a US-West region,
   with your ssh public key (`~/.ssh/id_ed25519.pub`) added, and the provider's automatic backups turned on.
   Then on the Mac add to `~/.ssh/config`:
   ```
   Host atlas-server
     HostName <the server's address>
     User root            # or the user the provider created (ubuntu, ...)
   ```
2. **Prepare it**: `deploy/server/atlas-server.sh provision`. It prints a Tailscale address to open once, to add
   the machine to your network, and a public key to add on GitHub as a read-only deploy key of the Roam backup
   repository.
3. **Subscription token** (Eli): in a terminal on the Mac run `claude setup-token`, sign in, and put the token it
   prints in `data/server.env` as `CLAUDE_CODE_OAUTH_TOKEN=...` (that file is untracked and is only sent to the
   server).
4. **Bridge on the Mac**: `bridge/install_bridge.sh` (already installed 2026-11-17). Put the Mac's Tailscale
   address (`tailscale ip -4`) in `config.server.toml`.
5. **Ship and test**: `atlas-server.sh deploy`, `atlas-server.sh env`, `atlas-server.sh check`. The check reads
   every source from the server, including the Mac ones through the bridge, and changes nothing.
6. **Move**: `atlas-server.sh cutover` shows what it will do; `cutover --yes` does it. Then set the URL in the
   iPhone app (Settings) to the address it prints.

## Common tasks

```bash
deploy/server/atlas-server.sh status            # where the agent is running, its health, free disk
deploy/server/atlas-server.sh deploy            # after committing: ship HEAD; restarts the agent when it is idle
deploy/server/atlas-server.sh env               # after changing a secret in data/.env or data/server.env (then deploy)
deploy/server/atlas-server.sh logs              # follow the agent's log
deploy/server/atlas-server.sh check             # every integration, as seen from the server
deploy/server/atlas-server.sh back-to-mac --yes # bring the agent home
bridge/install_bridge.sh                        # (re)install the Mac bridge;  tail -f ~/Library/Logs/ea-bridge.log
# Try the image on the Mac without a server (scratch state, another port):
docker build -f deploy/Dockerfile -t atlas-agent <a staged copy: see `deploy` in atlas-server.sh>
```

## Gotchas learned

- **Simulating a sleeping Mac** without closing the laptop: inside the container, point `sources.bridge_url` at a
  Tailscale address nobody answers on and call the connector (done 2026-11-17; a real overnight sleep has not
  been observed yet). Never let such a test write to the real `tracker_outbox.jsonl`.
- **Logs of the two cron jobs**: `/var/log/sync-tracker-copy.log`, `/var/log/follow-mac-timezone.log` on the server.

- **The Claude CLI will not run tools unprompted as root**, so the container runs as user 1000 and
  `/opt/atlas/data` belongs to that user. A root-owned file in there (copied by hand) makes the agent fail to write.
- **ssh needs a passwd entry for the user it runs as.** With only `USER 1000:1000` in the image, every Roam backup
  pull failed with "No user exists for uid 1000" (2026-11-17, found in `docker logs atlas`); the image now creates
  the user. Testing the deploy key from the server's own shell does not catch this.
- **The phone says "waiting for a connection"** when its URL (Settings in the app) still names the Mac, or when
  Tailscale is off on the phone. `tailscale status` on the server shows whether the phone has ever talked to it.
- **Docker publishes ports past ufw.** The compose file binds the port to 127.0.0.1 for that reason; do not
  change it to 0.0.0.0.
- **HOME is on the data volume** (`/data/home`). The CLI's sessions, the LLM inbox and the Roam key live under
  it; with the default home they would vanish with every rebuild and the agent would lose its conversation.
- **The session folder is named after the working directory**, which differs between the Mac and the server;
  `cutover` and `back-to-mac` copy between the two names.
- **The bridge token travels in `X-Bridge-Token`**, not `Authorization`, because the tracker's own token uses
  that header. The first bridge (2026-11-04) also dropped `X-Agent-Name`, so writes lost their attribution.
- **The App Store Tailscale's command line does not answer under launchd**; the bridge reads the Mac's address
  from `ifconfig` instead.
- **The Mac's night block (00:00 to 06:00) refuses connections to Tailscale addresses from the Mac**, and Docker
  Hub. Whether it also refuses the server's calls into the bridge has not been tested; if the Mac tools fail at
  night while the Mac is awake, that is where to look.
- **Playwright's bundled Node needs `libatomic1`**, which `python:3.12-slim` lacks; without it the image build
  stops with exit code 127 and no useful message unless built with `--progress=plain`.
- **A space after the `=` in `data/server.env`** stays in the value; the token line must have none.
- **DigitalOcean said "ssh key with fingerprint already exists"** and still did not list the key until the page
  was reloaded; the key was there. AWS Lightsail on a long-unused account has a vCPU quota of 0.
- **First value wins in a `.env` file**: `env` writes `data/server.env` before `data/.env` so the server's values
  override.
- **`tracker_close` through the bridge** (`POST /mac/close-tracker`) stops the tracker's launcher and then its
  Python by PID; stopping Python alone makes the launcher start it again.
- `tests/test_bridge.py` fails when the tracker connector gains a write the bridge does not allow; add it to
  `WRITE_ALLOW` in `bridge/mac_bridge.py` and reinstall the bridge.
