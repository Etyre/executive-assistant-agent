#!/usr/bin/env bash
# Everything about running the agent on a server, driven from the Mac. Guide: deploy/server/README.md.
#
#   atlas-server.sh provision        once, on a fresh Ubuntu 24.04 server: Docker, Tailscale, firewall, /opt/atlas
#   atlas-server.sh deploy           ship the committed code (HEAD), agent-coding/shared and both config files; build
#                                    the image; if the agent is already running there, restart it once it is idle
#   atlas-server.sh env              copy the secrets: data/server.env + data/.env -> /opt/atlas/data/.env
#   atlas-server.sh check            run `python -m ea check` on the server (reads only; safe before the move)
#   atlas-server.sh cutover --yes    MOVE the agent: stop it here, copy its state, start it there, point the Mac app at it
#   atlas-server.sh back-to-mac --yes   the reverse
#   atlas-server.sh status | logs | ssh
#
# The server is the ssh host `atlas-server` (an entry in ~/.ssh/config), or $ATLAS_SERVER (user@address).
# Only one copy of the agent may run at a time: cutover and back-to-mac stop one side before starting the other.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="${ATLAS_SERVER:-atlas-server}"
ROOT=/opt/atlas
LABEL=com.elityre.executive-assistant
DOMAIN="gui/$(id -u)"
SHARED="${EA_SHARED_DIR:-$REPO/../shared}"
# The Claude CLI files each session under a folder named after the agent's working directory (its data folder).
SESSIONS_MAC="$HOME/.claude/projects/$(printf '%s' "$REPO/data" | sed 's/[^a-zA-Z0-9]/-/g')"
SESSIONS_SRV="$ROOT/data/home/.claude/projects/-data"
COMPOSE="docker compose -f $ROOT/app/deploy/docker-compose.yml"
# State that is copied between the two machines. Secrets (.env), the server's home folder and the document copies
# are handled separately; logs stay where they were written.
# food_orders.jsonl and food/ are written on the Mac by the bridge even while the agent lives on the server: copying
# the server's stale ones back with --delete would erase the record of today's order.
DATA_EXCLUDES=(--exclude=.env --exclude=server.env --exclude=home/ --exclude=docs/ --exclude=logs/ --exclude='*.log' --exclude=food_orders.jsonl --exclude=food/ --exclude=.backup/)

say() { printf '\n== %s\n' "$*"; }
die() { echo "atlas-server: $*" >&2; exit 1; }
r() { ssh "$SERVER" "$@"; }
sudo_word() { r 'test "$(id -u)" = 0 || echo sudo'; }
mac_health() { curl -s --noproxy '*' -m 4 http://127.0.0.1:8800/health || true; }
srv_health() { r "curl -s -m 4 http://127.0.0.1:8800/health" 2>/dev/null || true; }
srv_running() { [ -n "$(r "$SUDO docker ps -q -f name=^atlas$ -f status=running" 2>/dev/null)" ]; }
srv_url() { r "tailscale status --json" | python3 -c 'import json,sys; print("http://" + json.load(sys.stdin)["Self"]["DNSName"].rstrip(".") + ":8800")'; }

wait_idle() {  # $1: mac_health | srv_health. A turn in progress is never cut off.
  local n=0
  while "$1" | grep -q '"busy": *true'; do
    [ $n -eq 0 ] && echo "the agent is in the middle of a turn; waiting for it to finish"
    n=$((n + 1)); sleep 5
  done
}

wait_healthy() {  # the server's agent answers /health within two minutes
  for _ in $(seq 1 40); do
    srv_health | grep -q '"ok"' && return 0
    sleep 3
  done
  return 1
}

cmd="${1:-}"; shift || true
case "$cmd" in provision|deploy|env|check|cutover|back-to-mac|status|logs|ssh) ;; *) sed -n '2,13p' "$0"; exit 1 ;; esac
r true || die "cannot ssh to '$SERVER' (add a Host atlas-server entry to ~/.ssh/config, or set ATLAS_SERVER=user@address)"
SUDO="$(sudo_word)"
RSYNC=(rsync -az --rsync-path="${SUDO:+sudo }rsync")

case "$cmd" in
provision)
  say "installing Docker, Tailscale and the firewall on $SERVER"
  r "$SUDO bash -s" <<'EOS'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
[ "$ID" = ubuntu ] || { echo "this expects Ubuntu 24.04 LTS (found $PRETTY_NAME)"; exit 1; }
apt-get update -qq
# Docker and Compose from Ubuntu's own archive; Tailscale from its apt repository (its documented manual install).
apt-get install -y -qq ca-certificates curl rsync ufw unattended-upgrades python3 docker.io docker-compose-v2 docker-buildx >/dev/null
if ! command -v tailscale >/dev/null; then
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
  apt-get update -qq && apt-get install -y -qq tailscale >/dev/null
fi
systemctl enable --now docker >/dev/null
mkdir -p /opt/atlas/app /opt/atlas/data/home/.ssh
# The container runs as user 1000, whatever that user is called on this machine.
chown -R 1000:1000 /opt/atlas/data && chmod 700 /opt/atlas/data /opt/atlas/data/home/.ssh
# 2 GB of swap on a small machine, so a burst (headless Chromium) does not get the agent killed.
if [ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 6000000 ] && ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
# Nothing comes in from the internet except ssh; the agent is reached over Tailscale only.
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null; ufw allow in on tailscale0 >/dev/null; ufw --force enable >/dev/null
# A read-only key for the Roam backup repository on GitHub (the agent pulls it every few hours).
K=/opt/atlas/data/home/.ssh
[ -f $K/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -C atlas-server -f $K/id_ed25519
grep -q github.com $K/known_hosts 2>/dev/null || ssh-keyscan -t ed25519 github.com >> $K/known_hosts 2>/dev/null
chown -R 1000:1000 $K
echo "installed: $(docker --version), $(docker compose version | head -1), tailscale $(tailscale version | head -1)"
EOS
  if ! r "tailscale status >/dev/null 2>&1"; then
    say "joining your Tailscale network: open the address printed below and approve the machine"
    r "$SUDO tailscale up --hostname=atlas"
  fi
  # The tools that moved here from the Mac keep the Mac's time zone (follow-mac-timezone.sh says why).
  scp -q "$REPO/deploy/server/follow-mac-timezone.sh" "$SERVER:/tmp/follow-mac-timezone.sh"
  # The read-only copy of the Project Tracker is refreshed from the Mac (sync-tracker-copy.sh says how).
  scp -q "$REPO/deploy/server/sync-tracker-copy.sh" "$SERVER:/tmp/sync-tracker-copy.sh"
  r "$SUDO install -m 755 /tmp/sync-tracker-copy.sh /opt/atlas/sync-tracker-copy.sh && echo '*/5 * * * * root /opt/atlas/sync-tracker-copy.sh >> /var/log/sync-tracker-copy.log 2>&1' | $SUDO tee /etc/cron.d/sync-tracker-copy >/dev/null"
  r "$SUDO install -m 755 /tmp/follow-mac-timezone.sh /opt/atlas/follow-mac-timezone.sh && echo '*/10 * * * * root /opt/atlas/follow-mac-timezone.sh >> /var/log/follow-mac-timezone.log 2>&1' | $SUDO tee /etc/cron.d/follow-mac-timezone >/dev/null"
  # The tailnet's port 8800 is handed to the agent, which itself listens on this machine only.
  r "$SUDO tailscale serve --bg --tcp=8800 tcp://localhost:8800" || echo "!! 'tailscale serve' failed; the agent will not be reachable from your devices until it works"
  say "done. The agent will be at $(srv_url) once it is moved here."
  echo "Read-only deploy key for the Roam backup (GitHub -> roam-backup-JSON-only -> Settings -> Deploy keys -> Add, no write access):"
  r "$SUDO cat $ROOT/data/home/.ssh/id_ed25519.pub"
  ;;

deploy)
  [ -f "$REPO/config.server.toml" ] || die "no config.server.toml (copy deploy/server/config.server.example.toml and set the Mac's Tailscale address)"
  [ -f "$SHARED/llm_contact.py" ] || die "agent-coding/shared not found at $SHARED"
  git -C "$REPO" diff --quiet HEAD -- ea instructions requirements.txt deploy || echo "note: uncommitted changes are NOT deployed; only HEAD is"
  stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
  git -C "$REPO" archive HEAD ea instructions requirements.txt deploy/Dockerfile deploy/docker-compose.yml .dockerignore | tar -x -C "$stage"
  mkdir "$stage/shared"
  cp "$SHARED/llm_contact.py" "$SHARED/llm-system-prompt-preamble.md" "$SHARED/contact_eli_tool.json" "$SHARED/note_to_eli_tool.json" "$stage/shared/"
  git -C "$REPO" log -1 --format='%h %s' > "$stage/VERSION"
  say "copying $(cat "$stage/VERSION") to $SERVER"
  "${RSYNC[@]}" --delete "$stage/" "$SERVER:$ROOT/app/"
  "${RSYNC[@]}" "$REPO/config.toml" "$REPO/config.server.toml" "$SERVER:$ROOT/"
  say "building the image (the first build takes several minutes)"
  r "$SUDO $COMPOSE build"
  if srv_running; then
    wait_idle srv_health
    say "restarting the agent on the server"
    r "$SUDO $COMPOSE up -d --force-recreate"
    wait_healthy || die "the agent did not come back; see: $0 logs"
    echo "running: $(r "cat $ROOT/app/VERSION")"
  else
    echo "built. Not started: the agent has not been moved to the server (that is '$0 cutover')."
  fi
  ;;

env)
  [ -f "$REPO/data/.env" ] || die "no data/.env"
  # server.env first: the first value found for a name wins, so anything in it overrides data/.env on the server.
  { [ -f "$REPO/data/server.env" ] && cat "$REPO/data/server.env"; echo; cat "$REPO/data/.env"; } \
    | r "$SUDO sh -c 'umask 077; cat > $ROOT/data/.env; chown 1000:1000 $ROOT/data/.env'"
  echo "secrets copied to $SERVER:$ROOT/data/.env (readable by the agent's user only)"
  ;;

check)
  r "$SUDO test -f $ROOT/data/.env" || die "no secrets on the server yet: $0 env"
  r "$SUDO $COMPOSE run --rm --no-deps ea python -m ea check"
  ;;

cutover)
  r "$SUDO docker image inspect atlas-agent >/dev/null 2>&1" || die "no image on the server yet: $0 deploy"
  srv_running && die "the agent is already running on the server"
  grep -q '^CLAUDE_CODE_OAUTH_TOKEN=..' "$REPO/data/server.env" 2>/dev/null \
    || die "data/server.env has no CLAUDE_CODE_OAUTH_TOKEN: the server could not use the Claude subscription (see deploy/server/README.md, step 3)"
  launchctl print "$DOMAIN/com.elityre.ea-bridge" >/dev/null 2>&1 || die "the Mac bridge is not installed: bridge/install_bridge.sh"
  if [ "${1:-}" != "--yes" ]; then
    cat <<EOF
This will, in order:
  1. wait until the agent is not in the middle of a turn, then stop it on this Mac (and keep it from starting at login)
  2. copy its state ($(du -sh "$REPO/data" | cut -f1)) and its saved sessions to $SERVER
  3. start it on the server; if it does not answer within two minutes, start it on this Mac again
  4. point the Mac app and the app's auto-deploy at the server
Run again with --yes to do it. '$0 back-to-mac --yes' undoes it.
EOF
    exit 0
  fi
  wait_idle mac_health
  say "stopping the agent on this Mac"
  launchctl disable "$DOMAIN/$LABEL"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  sleep 2; mac_health | grep -q '"ok"' && die "something still answers on port 8800 here; not copying while it runs"
  back() {  # anything failing from here on: the Mac runs the agent again
    echo "!! the move failed; starting the agent on this Mac again"
    r "$SUDO $COMPOSE down" >/dev/null 2>&1 || true
    launchctl enable "$DOMAIN/$LABEL"; launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$LABEL.plist" || true
  }
  trap back ERR
  say "copying the agent's state"
  "${RSYNC[@]}" --delete "${DATA_EXCLUDES[@]}" "$REPO/data/" "$SERVER:$ROOT/data/"
  r "$SUDO mkdir -p $SESSIONS_SRV '$ROOT/data/home/Library/Application Support/LLMInbox'"
  [ -d "$SESSIONS_MAC" ] && "${RSYNC[@]}" "$SESSIONS_MAC/" "$SERVER:$SESSIONS_SRV/"
  # How the LLM inbox notifies Eli (ntfy, email): the same settings as on the Mac.
  [ -f "$HOME/Library/Application Support/LLMInbox/config.env" ] && \
    r "$SUDO sh -c 'umask 077; cat > \"$ROOT/data/home/Library/Application Support/LLMInbox/config.env\"'" < "$HOME/Library/Application Support/LLMInbox/config.env"
  "$0" env
  r "$SUDO chown -R 1000:1000 $ROOT/data"
  say "starting the agent on the server"
  r "$SUDO $COMPOSE up -d"
  wait_healthy || { r "$SUDO docker logs --tail 40 atlas" || true; false; }
  trap - ERR
  URL="$(srv_url)"
  say "the agent is running on the server: $URL"
  pid="$(pgrep -x Atlas || true)"; [ -n "$pid" ] && kill $pid && sleep 1
  defaults write com.elityre.atlas baseURL "$URL"
  open -g /Applications/Atlas.app 2>/dev/null || true
  ATLAS_HEALTH_URL="$URL/health" "$REPO/apps/Atlas/install-auto-deploy.sh" >/dev/null
  echo "Mac app pointed at the server. On the iPhone: Atlas -> Settings -> URL -> $URL (Tailscale must be on)."
  ;;

back-to-mac)
  if [ "${1:-}" != "--yes" ]; then
    echo "This stops the agent on the server, copies its state back over $REPO/data (a copy of the current folder is kept beside it) and starts it on this Mac. Run again with --yes."
    exit 0
  fi
  mac_health | grep -q '"ok"' && die "the agent is already running on this Mac"
  if srv_running; then wait_idle srv_health; say "stopping the agent on the server"; r "$SUDO $COMPOSE stop"; fi
  r "$SUDO test -f $ROOT/data/state.db" || die "the server has no state to bring back"
  keep="$REPO/data.before-return-$(date +%Y%m%d-%H%M%S)"
  cp -cR "$REPO/data" "$keep"                 # an APFS clone: instant, and takes no space until files differ
  say "copying the agent's state back (the folder as it was: $keep)"
  "${RSYNC[@]}" --delete "${DATA_EXCLUDES[@]}" "$SERVER:$ROOT/data/" "$REPO/data/"
  mkdir -p "$SESSIONS_MAC" && "${RSYNC[@]}" "$SERVER:$SESSIONS_SRV/" "$SESSIONS_MAC/"
  say "starting the agent on this Mac"
  launchctl enable "$DOMAIN/$LABEL"
  launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$LABEL.plist"
  pid="$(pgrep -x Atlas || true)"; [ -n "$pid" ] && kill $pid && sleep 1
  defaults write com.elityre.atlas baseURL "http://127.0.0.1:8800"
  open -g /Applications/Atlas.app 2>/dev/null || true
  "$REPO/apps/Atlas/install-auto-deploy.sh" >/dev/null
  echo "back on the Mac. On the iPhone, set the URL in Atlas -> Settings back to the Mac's address."
  ;;

status)
  echo "server ($SERVER): $(srv_running && echo "running $(r "cat $ROOT/app/VERSION 2>/dev/null")" || echo "not running")"
  echo "  health: $(srv_health | head -c 300)"
  echo "  disk:   $(r "df -h $ROOT | tail -1 | awk '{print \$4 \" free of \" \$2}'")"
  echo "this Mac: $(mac_health | grep -q '"ok"' && echo "the agent is running here" || echo "the agent is not running here")"
  echo "  bridge: $(launchctl print "$DOMAIN/com.elityre.ea-bridge" 2>/dev/null | grep -m1 'state =' | xargs || echo "not installed")"
  echo "  backup of the server on this Mac: $("$REPO/.venv/bin/python" "$REPO/bridge/backup_server.py" --status 2>&1)"
  ;;

# The file outlives deploys (Docker's own log goes with the container); before the first deploy that writes it, Docker's.
logs) ssh -t "$SERVER" "if [ -f $ROOT/data/logs/atlas.log ]; then $SUDO tail -n ${1:-200} -F $ROOT/data/logs/atlas.log; else $SUDO docker logs -f --tail ${1:-200} atlas; fi" ;;
ssh) ssh -t "$SERVER" ;;
esac
