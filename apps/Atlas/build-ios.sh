#!/usr/bin/env bash
# Build the iPhone app (Release, automatic signing with the team in project.yml) and install it on Eli's iPhone
# over Wi-Fi or cable with devicectl. Installing over an existing copy is an update: settings and token stay.
#   ./build-ios.sh            full build (needs the App ID to have Push Notifications; Xcode registers it)
#   ./build-ios.sh --no-push  build without the aps-environment entitlement (installs with the team's wildcard
#                             profile even before the push App ID exists; the app works, only APNs is off)
#   ./build-ios.sh --no-install  build only
set -euo pipefail
cd "$(dirname "$0")"
DEVICE="${ATLAS_IPHONE_UDID:-$(xcrun devicectl list devices 2>/dev/null | grep physical | grep -o '[0-9A-F]\{8\}-[0-9A-F]\{16\}' | head -1)}"
NOPUSH=0; INSTALL=1
for a in "$@"; do case "$a" in --no-push) NOPUSH=1;; --no-install) INSTALL=0;; esac; done
[ -d Atlas.xcodeproj ] || xcodegen generate
mkdir -p build-ios
EXTRA=()
if [ "$NOPUSH" = 1 ]; then
  printf '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict/></plist>\n' > build-ios/nopush.entitlements
  printf 'CODE_SIGN_ENTITLEMENTS[sdk=iphoneos*] = build-ios/nopush.entitlements\n' > build-ios/nopush.xcconfig
  EXTRA=(-xcconfig build-ios/nopush.xcconfig)
else
  EXTRA=(-allowProvisioningUpdates -allowProvisioningDeviceRegistration)
fi
xcodebuild -project Atlas.xcodeproj -scheme Atlas -configuration Release -destination 'generic/platform=iOS' \
  -derivedDataPath build-ios "${EXTRA[@]}" build 2>&1 | grep -E "error:|BUILD" || true
APP=build-ios/Build/Products/Release-iphoneos/Atlas.app
[ -d "$APP" ] && [ "$APP/Atlas" -nt build-ios/nopush.xcconfig -o "$NOPUSH" = 0 ] || { echo "build failed"; exit 1; }
echo "built $APP (push entitlement: $([ "$NOPUSH" = 1 ] && echo off || echo on))"
[ "$INSTALL" = 1 ] || exit 0
[ -n "$DEVICE" ] || { echo "no paired iPhone found (xcrun devicectl list devices)"; exit 1; }
echo "installing on $DEVICE ..."
xcrun devicectl device install app --device "$DEVICE" "$APP" 2>&1 | grep -E "installed|error|Error|unavailable|App is" | head -5
