#!/usr/bin/env bash
# Build the Mac app (Release), sign it with a stable identity, and relaunch it.
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
xattr -cr "$APP"
codesign --force --deep --options runtime -s "$IDENTITY" "$APP"
echo "signed with: $IDENTITY"
if [ "${1:-}" != "--no-launch" ]; then
  pkill -f "Atlas.app/Contents/MacOS/Atlas" || true
  sleep 1
  open "$APP"
  echo "relaunched $APP"
fi
