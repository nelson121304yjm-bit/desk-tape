#!/usr/bin/env python3
"""Re-test every data link Desk Tape depends on.

Hits the running local server (default http://127.0.0.1:8787) and prints one
row per symbol plus the sentiment / headlines block, so a broken feed is
visible at a glance. Exit code 1 if any link is unhealthy.

Usage:
    python3.12 scripts/check_feeds.py [base_url]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

SYMBOLS = [
    "^GSPC",
    "^IXIC",
    "^DJI",
    "^VIX",
    "BTC-USD",
    "GC=F",
    "CL=F",
    "^TNX",
    "DX-Y.NYB",
    "EURUSD=X",
    "^HSI",
    "^N225",
]


def fetch(url: str, timeout: float = 25.0) -> tuple[object, float]:
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload, time.perf_counter() - started


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787").rstrip("/")
    query = urllib.parse.urlencode({"symbols": ",".join(SYMBOLS), "hero": "^GSPC"})
    url = f"{base}/api/wallpaper?{query}"

    try:
        payload, elapsed = fetch(url)
    except Exception as error:  # noqa: BLE001 - reported to the user
        print(f"FAIL  wallpaper endpoint: {error}")
        return 1

    quotes = {quote.get("symbol"): quote for quote in payload.get("quotes", [])}
    print(f"wallpaper endpoint  {elapsed * 1000:6.0f} ms   {url}")
    print(f"sources: {', '.join(payload.get('sources') or []) or '—'}   errors: {payload.get('errors') or 'none'}")
    print()
    print(f"{'SYMBOL':<10}{'PRICE':>12}{'CHG%':>9}{'POINTS':>8}  {'SERIES':<7}{'STATUS':<12}NAME")

    problems: list[str] = []
    for symbol in SYMBOLS:
        quote = quotes.get(symbol)
        if not quote:
            problems.append(f"{symbol}: missing from payload")
            print(f"{symbol:<10}{'—':>12}{'—':>9}{0:>8}  {'—':<7}{'MISSING':<12}")
            continue
        points = len(quote.get("series") or [])
        price = quote.get("price")
        pct = quote.get("changePct")
        if not isinstance(price, (int, float)):
            problems.append(f"{symbol}: no price")
        if points < 2:
            problems.append(f"{symbol}: {points} series points")
        print(
            f"{symbol:<10}{price if isinstance(price, (int, float)) else '—':>12}"
            f"{(f'{pct:+.2f}%' if isinstance(pct, (int, float)) else '—'):>9}"
            f"{points:>8}  {str(quote.get('seriesSource') or '—'):<7}"
            f"{str(quote.get('marketStatus') or '—'):<12}"
            f"{quote.get('name') or quote.get('label') or ''}"
        )

    sentiment_started = time.perf_counter()
    try:
        sentiment, _ = fetch(f"{base}/api/market-sentiment")
        sent_ms = (time.perf_counter() - sentiment_started) * 1000
    except Exception as error:  # noqa: BLE001
        sentiment, sent_ms = {}, 0
        problems.append(f"market-sentiment: {error}")

    embedded = payload.get("sentiment") or {}
    print()
    print("sentiment")
    print(
        f"  wallpaper payload: score={embedded.get('score')} band={embedded.get('band')} "
        f"label={embedded.get('label')} detail={embedded.get('detail')}"
    )
    print(f"  /api/market-sentiment latency={sent_ms:.0f} ms")
    gauges = sentiment.get("gauges") or []
    if gauges:
        for gauge in gauges[:6]:
            if not isinstance(gauge, dict):
                continue
            print(
                f"  · {gauge.get('label'):<14} score={gauge.get('score')} "
                f"band={gauge.get('band')} ({gauge.get('detail')})"
            )
    if not isinstance(embedded.get("score"), (int, float)):
        problems.append("sentiment: wallpaper payload has no score")
    if not gauges:
        problems.append("sentiment: /api/market-sentiment has no gauges")

    headlines = payload.get("headlines") or []
    print()
    print(f"headlines: {len(headlines)}")
    for item in headlines[:3]:
        title = item.get("title") if isinstance(item, dict) else str(item)
        source = item.get("source") if isinstance(item, dict) else ""
        print(f"  · {title}" + (f"  [{source}]" if source else ""))
    if not headlines:
        problems.append("headlines: empty")

    print()
    if problems:
        for problem in problems:
            print(f"FAIL  {problem}")
        return 1
    print(f"OK    all {len(SYMBOLS)} symbols priced with intraday series, sentiment and headlines present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
