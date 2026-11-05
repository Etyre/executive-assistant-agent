#!/usr/bin/env bash
# Installs the agent as a launchd agent on a Mac (the Mac mini). Secrets are read from data/.env.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
[ -d .venv ] || /opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p data && chmod 700 data
[ -f data/.env ] || { cp .env.example data/.env; echo "fill in data/.env, then re-run"; exit 1; }
[ -f config.toml ] || { cp config.example.toml config.toml; echo "edit config.toml (leave sources.bridge_url empty), then re-run"; exit 1; }
PLIST="$HOME/Library/LaunchAgents/com.elityre.executive-assistant.plist"
sed "s#__REPO__#$REPO#g" deploy/macmini/com.elityre.executive-assistant.plist > "$PLIST"
launchctl bootout "gui/$(id -u)/com.elityre.executive-assistant" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed; log: $REPO/data/ea.log"
