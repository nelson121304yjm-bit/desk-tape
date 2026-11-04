#!/bin/sh
# Start the local data server (if needed) and open Desk Tape.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${DESKTAPE_PORT:-8787}"
PYTHON="${PYTHON:-python3.12}"

if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "starting data server on 127.0.0.1:$PORT…"
  nohup "$PYTHON" desk_tape_server.py --port "$PORT" >/tmp/desktape-server.log 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi

if [ -d "$HOME/Applications/DeskTape.app" ]; then
  open "$HOME/Applications/DeskTape.app"
else
  echo "DeskTape.app is not installed yet; run ./scripts/install-wallpaper-app.sh" >&2
  exit 1
fi
