#!/bin/sh
# Keep the local data server running across logins via a LaunchAgent.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${DESKTAPE_PORT:-8787}"
PYTHON="${PYTHON:-python3.12}"
LABEL="com.desktape.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/DeskTapeServer.log"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3.12 || command -v python3)"
fi

cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/desk_tape_server.py</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST_END

chmod 600 "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "loaded $LABEL → http://127.0.0.1:$PORT/wallpaper"
