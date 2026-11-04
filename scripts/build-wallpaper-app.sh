#!/usr/bin/env bash
# Build build/DeskTape.app — a menu-bar app that draws the wallpaper behind the desktop icons.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3.12}"
PYTHON_BIN="$(command -v "$PYTHON" || true)"
APP="build/DeskTape.app"

if [ -z "$PYTHON_BIN" ]; then
  echo "error: $PYTHON not found on PATH" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c "import AppKit" >/dev/null 2>&1; then
  echo "error: $PYTHON cannot import AppKit." >&2
  echo "       pip install pyobjc-framework-Cocoa  (or set PYTHON=/path/to/python3)" >&2
  exit 1
fi

# Icon: rendered from geometry so the repo stays free of binary assets.
if "$PYTHON_BIN" -c "import PIL" >/dev/null 2>&1; then
  "$PYTHON_BIN" scripts/make-icon.py >/dev/null
else
  echo "notice: Pillow missing, keeping any existing build/AppIcon.icns" >&2
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp desktop/desk_tape.py "$APP/Contents/Resources/desk_tape.py"
if [ -f build/AppIcon.icns ]; then
  cp build/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
fi

cat > "$APP/Contents/MacOS/DeskTape" <<EOF
#!/bin/sh
exec "$PYTHON_BIN" "\$(dirname "\$0")/../Resources/desk_tape.py" "\$@"
EOF
chmod +x "$APP/Contents/MacOS/DeskTape"

"$PYTHON" - "$APP/Contents/Info.plist" <<'PY'
import plistlib
import sys

payload = {
    "CFBundleName": "DeskTape",
    "CFBundleDisplayName": "Desk Tape",
    "CFBundleIdentifier": "com.openmarket.desktape",
    "CFBundleExecutable": "DeskTape",
    "CFBundleIconFile": "AppIcon",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1.0",
    "CFBundleVersion": "1",
    "LSUIElement": True,
    "NSHighResolutionCapable": True,
    "LSMinimumSystemVersion": "11.0",
    "NSHumanReadableCopyright": "Open Market Terminal",
}
with open(sys.argv[1], "wb") as handle:
    plistlib.dump(payload, handle)
PY

codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "built $APP"
echo "run:   open $APP --args --mode horizon"
echo "install: ./scripts/install-wallpaper-app.sh   # ~/Applications + Desktop launcher"
