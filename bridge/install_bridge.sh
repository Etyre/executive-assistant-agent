#!/usr/bin/env bash
# Installs the Mac bridge as a launchd agent. Run from the repo root on the Mac that holds the data.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN="${EA_BRIDGE_TOKEN:-$(openssl rand -hex 32)}"
PLIST="$HOME/Library/LaunchAgents/com.elityre.ea-bridge.plist"
mkdir -p "$HOME/Library/Logs"
sed -e "s#__REPO__#$REPO#g" -e "s#__TOKEN__#$TOKEN#g" -e "s#__HOME__#$HOME#g" "$REPO/bridge/com.elityre.ea-bridge.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/com.elityre.ea-bridge" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "bridge installed. token (put this in the cloud agent's EA_BRIDGE_TOKEN):"
echo "$TOKEN"
echo "log: $HOME/Library/Logs/ea-bridge.log   tailscale ip: $(tailscale ip -4 2>/dev/null || echo '?')"
