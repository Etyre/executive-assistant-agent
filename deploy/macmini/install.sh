#!/usr/bin/env bash
# Installs (or re-installs) the agent as the launchd agent com.elityre.executive-assistant on this Mac. Idempotent:
# rerun after any code change to restart it. Secrets are read from data/.env; the log is ~/Library/Logs/executive-assistant.log.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
[ -d .venv ] || /opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p data && chmod 700 data
[ -f data/.env ] || { cp .env.example data/.env; echo "fill in data/.env, then re-run"; exit 1; }
[ -f config.toml ] || { cp config.example.toml config.toml; echo "edit config.toml (leave sources.bridge_url empty), then re-run"; exit 1; }
PLIST="$HOME/Library/LaunchAgents/com.elityre.executive-assistant.plist"
mkdir -p "$HOME/Library/Logs"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" deploy/macmini/com.elityre.executive-assistant.plist > "$PLIST"
launchctl bootout "gui/$(id -u)/com.elityre.executive-assistant" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed; log: $HOME/Library/Logs/executive-assistant.log"
