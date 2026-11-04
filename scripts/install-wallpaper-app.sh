#!/usr/bin/env bash
# Build, install, and make Desk Tape easy to find.
#
#   ./scripts/install-wallpaper-app.sh            # ~/Applications + Desktop launcher
#   ./scripts/install-wallpaper-app.sh --system   # /Applications (needs admin)
#
# The app is menu-bar only by design (LSUIElement), so it never shows a Dock
# icon; the Desktop launcher and Spotlight are how you open it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3.12}"
TARGET="/Applications/DeskTape.app"

if [ "${1:-}" != "--system" ]; then
  TARGET="$HOME/Applications/DeskTape.app"
fi

./scripts/build-wallpaper-app.sh

mkdir -p "$(dirname "$TARGET")"
rm -rf "$TARGET"
if ! cp -R build/DeskTape.app "$TARGET" 2>/dev/null; then
  echo "error: cannot write $TARGET" >&2
  echo "       macOS may need you to allow it, or try: sudo ./scripts/install-wallpaper-app.sh --system" >&2
  exit 1
fi

"$PYTHON" scripts/make-launcher.py "$HOME/Desktop/Desk Tape.app" "$TARGET"

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then
  "$LSREGISTER" -f "$TARGET" || true
  "$LSREGISTER" -f "$HOME/Desktop/Desk Tape.app" || true
fi

echo "installed: $TARGET"
echo "desktop:   $HOME/Desktop/Desk Tape.app"
echo "menu bar:  small chart icon at the right; click it to open the settings panel"
open "$TARGET"
