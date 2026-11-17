#!/usr/bin/env bash
# Runs on the server every ten minutes (cron, installed by `atlas-server.sh provision`). The tools that moved
# here from the Mac (Project Tracker, Sleep Bandit) work in local time: their days, work hours and 4 AM rollover.
# On the Mac they followed its clock when Eli travelled; here they follow the Mac's time zone, read from the
# bridge's /health. A change rewrites that tool's env file and recreates its container. Mac asleep: nothing happens.
set -u
BRIDGE="$(sed -n 's/^bridge_url *= *"\(.*\)".*/\1/p' /opt/atlas/config.server.toml | head -1)"
[ -n "$BRIDGE" ] || exit 0
TZ_NAME="$(curl -s -m 8 "$BRIDGE/health" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tz",""))' 2>/dev/null)"
[ -n "$TZ_NAME" ] && [ -f "/usr/share/zoneinfo/$TZ_NAME" ] || exit 0
follow() {  # follow <root> <VAR> <compose file> <container>
  [ -f "$1/env" ] || return 0
  grep -qx "$2=$TZ_NAME" "$1/env" && return 0
  { grep -v "^$2=" "$1/env"; echo "$2=$TZ_NAME"; } > "$1/env.tmp" && mv "$1/env.tmp" "$1/env"
  echo "$(date "+%F %T") $4: time zone is now $TZ_NAME"
  [ -z "$(docker ps -q -f "name=^$4$")" ] || docker compose -f "$3" --env-file "$1/env" up -d >/dev/null 2>&1
}
follow /opt/sleep-bandit SB_TZ /opt/sleep-bandit/app/deploy/tailnet/docker-compose.yml bandit
follow /opt/tracker TRACKER_TZ /opt/tracker/app/deploy/tailnet/docker-compose.yml tracker
