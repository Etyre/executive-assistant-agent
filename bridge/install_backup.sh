#!/usr/bin/env bash
# Installs (or reinstalls) the launchd agent com.elityre.ea-backup, which copies the agent's conversations and memory
# from the server to this Mac at login and every six hours (bridge/backup_server.py). Idempotent.
#   ./bridge/install_backup.sh --uninstall   stops and removes it (the backups already made stay)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.elityre.ea-backup
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break; sleep 1; done
if [ "${1:-}" = "--uninstall" ]; then rm -f "$PLIST"; echo "removed $LABEL"; exit 0; fi
mkdir -p "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/bridge/$LABEL.plist" > "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL (runs now, then every six hours); log: $HOME/Library/Logs/ea-backup.log"
