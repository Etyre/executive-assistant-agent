#!/bin/bash
# Installs (or reinstalls) the launchd agent com.elityre.atlas-app-deploy, which runs auto_deploy.py: every new
# commit of the Mac app is built and put in /Applications as soon as the agent is not mid-turn. Idempotent.
#   state: ~/Library/Application Support/AtlasAppDeploy/     log: ~/Library/Logs/atlas-app-deploy.log
#   ./install-auto-deploy.sh --uninstall   stops and removes it
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.elityre.atlas-app-deploy
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/atlas-app-deploy.log"
# Not /usr/bin/python3: launchd denies that binary access to ~/Documents (see agent-coding/CLAUDE.md).
PY=/opt/homebrew/bin/python3

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then rm -f "$PLIST"; echo "removed $LABEL"; exit 0; fi

mkdir -p "$HOME/Library/Application Support/AtlasAppDeploy" "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$HERE/auto_deploy.py</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>ProcessType</key><string>Background</string>
</dict></plist>
PLIST
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; log: $LOG"
