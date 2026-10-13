#!/usr/bin/env bash
# Build the Thrash menu-bar app from menubar/Thrash.swift.
# Produces:
#   ~/.local/bin/Thrash          (bare binary, for running directly)
#   /Applications/Thrash.app     (bundle, for LaunchServices / open -a)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/menubar/Thrash.swift"
BIN="$HOME/.local/bin/Thrash"
APP="/Applications/Thrash.app"
APPBIN="$APP/Contents/MacOS/Thrash"

[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }
mkdir -p "$HOME/.local/bin" "$APP/Contents/MacOS"

echo "=== compiling menu-bar binary ==="
swiftc -O -o "$BIN" "$SRC"
echo "  binary: $BIN"

echo "=== building app bundle ==="
swiftc -O -o "$APPBIN" "$SRC"

cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Thrash</string>
    <key>CFBundleDisplayName</key>
    <string>Thrash</string>
    <key>CFBundleIdentifier</key>
    <string>com.cole.thrash.app</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>Thrash</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticTermination</key>
    <false/>
</dict>
</plist>
PLIST

touch "$APP"

echo "=== done ==="
echo "  run: open -a Thrash   (or $BIN)"
