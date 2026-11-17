#!/usr/bin/env bash
# Installs (or reinstalls) the Mac bridge as the launchd agent com.elityre.ea-bridge. Idempotent.
# The token lives in data/.env as EA_BRIDGE_TOKEN (created here once); the server gets the same file.
#   ./bridge/install_bridge.sh --uninstall   stops and removes it
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.elityre.ea-bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then rm -f "$PLIST"; echo "removed $LABEL"; exit 0; fi

ENV="$REPO/data/.env"
if ! grep -q '^EA_BRIDGE_TOKEN=..' "$ENV" 2>/dev/null; then
  # drop an empty EA_BRIDGE_TOKEN= line if there is one, then add a fresh token
  [ -f "$ENV" ] && { grep -v '^EA_BRIDGE_TOKEN=' "$ENV" > "$ENV.tmp" || true; mv "$ENV.tmp" "$ENV"; }
  echo "EA_BRIDGE_TOKEN=$(openssl rand -hex 32)" >> "$ENV"; chmod 600 "$ENV"
  echo "wrote a new EA_BRIDGE_TOKEN to data/.env"
fi
mkdir -p "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/bridge/$LABEL.plist" > "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; log: $HOME/Library/Logs/ea-bridge.log"
