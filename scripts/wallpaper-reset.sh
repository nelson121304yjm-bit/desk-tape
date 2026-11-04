#!/bin/sh
# Kill every Desk Tape instance, rebuild from source, reinstall and relaunch.
# Use this when the desktop still looks wrong: an old process from a previous
# build keeps its own window layer and would keep covering folders/widgets.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "stopping existing instances…"
pkill -f "DeskTape.app/Contents" || true
pkill -f "desk_tape.py" || true
sleep 1

echo "installing…"
./scripts/install-wallpaper-app.sh

echo
echo "waiting for launch…"
sleep 5
"$(command -v python3.12 || command -v python3)" scripts/wallpaper_status.py
