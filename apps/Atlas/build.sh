#!/usr/bin/env bash
# Build the Mac app (Release), sign it with a stable identity, install it in /Applications and relaunch it
# (--no-launch: build and sign only).
# A stable signature matters: macOS only grants notification permission to an app whose signature stays the same
# between launches; ad-hoc signing (-s -) changes it on every build. Uses the Apple Development certificate Xcode
# created for your Apple ID (free account is enough); override with ATLAS_SIGN_IDENTITY.
set -euo pipefail
cd "$(dirname "$0")"
IDENTITY="${ATLAS_SIGN_IDENTITY:-$(security find-identity -v -p codesigning | grep -o '"Apple Development: [^"]*"' | head -1 | tr -d '"')}"
[ -n "$IDENTITY" ] || { echo "no Apple Development identity in the keychain; sign in to Xcode once (Settings -> Accounts) or set ATLAS_SIGN_IDENTITY"; exit 1; }
[ -d Atlas.xcodeproj ] || xcodegen generate
xcodebuild -project Atlas.xcodeproj -scheme Atlas -configuration Release -destination 'platform=macOS' \
  -derivedDataPath build CODE_SIGNING_ALLOWED=NO build 2>&1 | grep -E "error:|warning: .*Atlas/|BUILD" || true
APP=build/Build/Products/Release/Atlas.app
[ -d "$APP" ] || { echo "build failed"; exit 1; }
# Sign a copy outside ~/Documents: there the file provider puts Finder attributes back on the bundle between
# `xattr -cr` and `codesign`, and codesign then refuses it ("detritus not allowed", seen 2026-11-15).
STAGE="$(mktemp -d)/Atlas.app"
ditto "$APP" "$STAGE"
xattr -cr "$STAGE"
codesign --force --deep --options runtime -s "$IDENTITY" "$STAGE"
codesign --verify --deep --strict "$STAGE"
echo "signed with: $IDENTITY"
rm -rf "$APP" && ditto "$STAGE" "$APP"   # keep the signed copy where the docs say the build is
if [ "${1:-}" != "--no-launch" ]; then
  # Install over the copy in /Applications (the login item) and run that one. Launching from build/ instead left
  # /Applications three days stale, so the next login would have brought back an old app (found 2026-11-15).
  # ATLAS_WAIT_IDLE=1 (set by auto_deploy.py): hold the swap until the agent is not mid-turn, so a reply being
  # streamed into the app is not cut off. A server that does not answer is not running a turn.
  if [ "${ATLAS_WAIT_IDLE:-}" = 1 ]; then
    while curl -s --max-time 3 "${ATLAS_HEALTH_URL:-http://127.0.0.1:8800/health}" | grep -q '"busy": *true'; do sleep 3; done
  fi
  for pid in $(pgrep -f "Atlas.app/Contents/MacOS/Atlas" || true); do kill "$pid" || true; done
  sleep 1
  if pgrep -f "Atlas.app/Contents/MacOS/Atlas" >/dev/null; then echo "the old app is still running; not replacing it"; exit 1; fi
  INSTALLED="${ATLAS_INSTALL_DIR:-/Applications}/Atlas.app"
  rm -rf "$INSTALLED.new" && ditto "$STAGE" "$INSTALLED.new" && rm -rf "$INSTALLED" && mv "$INSTALLED.new" "$INSTALLED"
  codesign --verify --deep --strict "$INSTALLED"
  open "$INSTALLED"
  echo "installed and relaunched $INSTALLED"
fi
rm -rf "$(dirname "$STAGE")"
