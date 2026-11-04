#!/usr/bin/env python3
"""Desk Tape's own data and page server (stdlib only).

    python3.12 desk_tape_server.py            # http://127.0.0.1:8787/wallpaper
    python3.12 desk_tape_server.py --port 8790

Routes
    /wallpaper, /desk      the wallpaper page
    /api/wallpaper         quotes + 5D series + sessions + sentiment + headlines
    /api/wallpaper/candles one symbol's OHLC bars
    /health                liveness
    everything else        static files from public/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
sys.path.insert(0, str(ROOT))

import wallpaper_data  # noqa: E402  (path set above so the script runs from anywhere)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DeskTape/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        if os.environ.get("DESKTAPE_LOG_REQUESTS"):
            super().log_message(fmt, *args)

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json({"error": "not found"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/wallpaper", "/wallpaper/", "/desk", "/desk/"):
                self.send_file(PUBLIC / "wallpaper.html")
                return
            if path == "/health":
                self.send_json({"ok": True})
                return
            if path == "/api/wallpaper":
                symbols = (query.get("symbols") or [""])[0]
                hero = (query.get("hero") or [""])[0]
                self.send_json(wallpaper_data.live_wallpaper(symbols, hero))
                return
            if path == "/api/wallpaper/candles":
                symbol = (query.get("symbol") or ["^GSPC"])[0]
                bars = int((query.get("bars") or ["84"])[0])
                interval = int((query.get("interval") or ["15"])[0])
                range_value = (query.get("range") or ["5D"])[0]
                self.send_json(
                    wallpaper_data.wallpaper_candles(symbol, range_value, interval, max(10, min(400, bars)))
                )
                return
        except Exception as exc:  # keep the wallpaper alive; report the fault
            self.send_json({"error": str(exc)}, status=500)
            return

        target = (PUBLIC / path.lstrip("/")).resolve()
        if not str(target).startswith(str(PUBLIC.resolve())) or not target.is_file():
            self.send_json({"error": "not found"}, status=404)
            return
        self.send_file(target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Desk Tape data + page server")
    parser.add_argument("--host", default=os.environ.get("DESKTAPE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DESKTAPE_PORT", "8787")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Desk Tape on http://{args.host}:{args.port}/wallpaper", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
