#!/bin/bash
# Builds, locally signs, installs, and launches the profile-free Mac app.

set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Pdf2Podcast"
BUNDLE_ID="io.pdf2podcast.mac"
LOCAL_IDENTITY="Pdf2Podcast Local Signing"
KEYCHAIN="${HOME}/Library/Keychains/login.keychain-db"
DERIVED_DATA="${DERIVED_DATA:-/tmp/Pdf2PodcastMacLocalDerived}"
DEST="${HOME}/Applications/${APP_NAME}.app"

identity="$(security find-identity "$KEYCHAIN" 2>/dev/null | grep -F "$LOCAL_IDENTITY" | head -1 | awk '{print $2}' || true)"
if [[ -z "$identity" ]]; then
    ./tools/make-signing-cert.sh
    identity="$(security find-identity "$KEYCHAIN" 2>/dev/null | grep -F "$LOCAL_IDENTITY" | head -1 | awk '{print $2}')"
fi

xcodebuild -quiet -project Pdf2PodcastMac.xcodeproj -scheme Pdf2PodcastMac \
    -configuration Debug -destination 'platform=macOS,variant=Mac Catalyst' \
    CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO \
    -derivedDataPath "$DERIVED_DATA" build

APP="$(find "$DERIVED_DATA/Build/Products" -maxdepth 2 -name "${APP_NAME}.app" -type d -print -quit)"
[[ -n "$APP" ]] || { echo "The build produced no app bundle." >&2; exit 1; }

while IFS= read -r -d '' nested; do
    codesign --force --sign "$identity" --timestamp=none "$nested" >/dev/null
done < <(find "$APP/Contents" -depth \
    \( -type d \( -name '*.framework' -o -name '*.appex' -o -name '*.xpc' \) \
       -o -type f -name '*.dylib' \) -print0)

codesign --force --sign "$identity" --identifier "$BUNDLE_ID" \
    --entitlements Pdf2PodcastMac.entitlements --timestamp=none "$APP" >/dev/null
codesign --verify --deep --strict "$APP"

osascript -e "tell application id \"$BUNDLE_ID\" to quit" >/dev/null 2>&1 || true
for _ in {1..30}; do pgrep -x "$APP_NAME" >/dev/null || break; sleep 0.1; done
pkill -x "$APP_NAME" >/dev/null 2>&1 || true
mkdir -p "${HOME}/Applications" "${HOME}/.Trash"
if [[ -e "$DEST" ]]; then
    mv "$DEST" "${HOME}/.Trash/${APP_NAME}-previous-$(date +%Y%m%dT%H%M%S).app"
fi
ditto "$APP" "$DEST"
open "$DEST"
echo "Installed and launched ${DEST} with ${LOCAL_IDENTITY}."
