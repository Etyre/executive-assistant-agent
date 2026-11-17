#!/usr/bin/env bash
# Runs on the server every five minutes (cron). The Project Tracker lives on Eli's Mac; this keeps the read-only
# copy beside the agent (/opt/tracker, the tracker project's deploy/tailnet) in step with it, so the agent can
# read tasks while the Mac is asleep. It asks the Mac bridge for /tracker-copy: a tar of a database snapshot,
# config.toml and the two documents. Unchanged (304) just records that the copy is current; changed, the copy is
# stopped, its files replaced and started again (a few seconds, and only while the Mac is awake, which is when
# the agent is talking to the Mac and not to the copy). Mac asleep: nothing happens and the copy stays as it is.
set -u
ROOT=/opt/tracker
[ -f $ROOT/app/deploy/tailnet/docker-compose.yml ] || exit 0          # the tracker's code has not been shipped yet
BRIDGE="$(sed -n 's/^bridge_url *= *"\(.*\)".*/\1/p' /opt/atlas/config.server.toml | head -1)"
TOKEN="$(sed -n 's/^EA_BRIDGE_TOKEN=//p' /opt/atlas/data/.env | head -1)"
[ -n "$BRIDGE" ] && [ -n "$TOKEN" ] || exit 0
COMPOSE="docker compose -f $ROOT/app/deploy/tailnet/docker-compose.yml --env-file $ROOT/env"
etag="$(cat $ROOT/etag 2>/dev/null || true)"
code="$(curl -s -m 120 -o $ROOT/incoming.tar -D $ROOT/incoming.headers -w '%{http_code}' -H "X-Bridge-Token: $TOKEN" ${etag:+-H "If-None-Match: $etag"} "$BRIDGE/tracker-copy")"
stamp() { date +%s > $ROOT/data/replica_synced_at; chown 1000:1000 $ROOT/data/replica_synced_at; }
case "$code" in
304) stamp ;;
200)
  rm -rf $ROOT/incoming && mkdir $ROOT/incoming && tar -xf $ROOT/incoming.tar -C $ROOT/incoming --no-same-owner || { echo "$(date "+%F %T") bad tar from the Mac"; exit 1; }
  [ -s $ROOT/incoming/tracker.sqlite3 ] || { echo "$(date "+%F %T") no database in the tar"; exit 1; }
  $COMPOSE stop -t 5 >/dev/null 2>&1
  rm -f $ROOT/data/tracker.sqlite3-wal $ROOT/data/tracker.sqlite3-shm
  mv -f $ROOT/incoming/* $ROOT/data/ && stamp
  chown -R 1000:1000 $ROOT/data && chmod 700 $ROOT/data
  $COMPOSE up -d >/dev/null 2>&1 || echo "$(date "+%F %T") the copy did not start"
  sed -n 's/^[Ee][Tt]ag: *//p' $ROOT/incoming.headers | tr -d '\r' > $ROOT/etag
  echo "$(date "+%F %T") copy updated from the Mac ($(du -h $ROOT/data/tracker.sqlite3 | cut -f1))"
  ;;
000) ;;                                                              # the Mac is asleep or away
*) echo "$(date "+%F %T") the bridge answered $code" ;;
esac
rm -rf $ROOT/incoming $ROOT/incoming.tar $ROOT/incoming.headers
