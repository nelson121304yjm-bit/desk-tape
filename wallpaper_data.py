"""Live market data for the Desk Tape wallpaper.

Everything here is keyless and public: CNBC's quote and chart services are the
primary feed, Yahoo is only a fallback for a symbol CNBC does not cover. Each
public value is normalised into the shape the page consumes, and nothing is
invented — a symbol without data comes back with an empty series, not a fake
one.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import concurrent.futures
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

USER_AGENT = "Desk-Tape/1.0 (+https://github.com/nelson121304yjm-bit/desk-tape)"


def normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def fetch_json_with_timeout(url: str, timeout: float = 8.0) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


CACHE: dict[tuple[Any, ...], tuple[float, Any]] = {}
CACHE_MAX_ENTRIES = int(os.environ.get("DESKTAPE_CACHE_MAX_ENTRIES", "2000"))


def _evict_cache(now: float) -> None:
    expired = [key for key, (stamp, _) in CACHE.items() if now - stamp > 3600]
    for key in expired[: CACHE_MAX_ENTRIES // 2]:
        CACHE.pop(key, None)
    overflow = len(CACHE) - CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest = sorted(CACHE.items(), key=lambda item: item[1][0])[: overflow + CACHE_MAX_ENTRIES // 20]
        for key, _ in oldest:
            CACHE.pop(key, None)


def cached(key: tuple[Any, ...], ttl: int, loader: Any) -> Any:
    now = time.time()
    hit = CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = loader()
    CACHE[key] = (now, value)
    if len(CACHE) > CACHE_MAX_ENTRIES:
        _evict_cache(now)
    return value


def live_quote(symbols_param: str, provider: str = "auto") -> dict[str, Any]:
    """Fallback quote lookup used only when CNBC has no row for a symbol."""
    url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=" + quote(symbols_param, safe=",^=.-")
    try:
        return fetch_json_with_timeout(url, 6.0) or {}
    except Exception:
        return {}


SENTIMENT_BANDS = ((25, "FEAR"), (45, "CAUTIOUS"), (60, "NEUTRAL"), (80, "GREED"), (101, "EUPHORIA"))

NEWS_FEED = "https://news.google.com/rss/search?q=stock%20market%20OR%20wall%20street&hl=en-US&gl=US&ceid=US:en"


def live_market_sentiment() -> dict[str, Any]:
    """A transparent risk-appetite gauge derived from the quotes we already hold."""
    rows = wallpaper_cnbc_quotes(["^VIX", "^GSPC", "^IXIC", "BTC-USD"])

    def change_pct(symbol: str) -> float:
        return safe_float((rows.get(symbol) or {}).get("change_pct")) or 0.0

    vix = safe_float((rows.get("^VIX") or {}).get("last")) or 18.0
    score = 50 + 9 * change_pct("^GSPC") + 6 * change_pct("^IXIC") + 1.5 * change_pct("BTC-USD") - 1.5 * (vix - 17)
    score = max(0.0, min(100.0, score))
    band = next(name for limit, name in SENTIMENT_BANDS if score < limit)
    return {
        "source": "desk-tape",
        "gauges": [
            {
                "key": "global",
                "label": "GLOBAL RISK",
                "score": round(score, 1),
                "band": band,
                "detail": f"VIX {vix:.1f}",
            }
        ],
        "errors": [],
    }


def _rss_items(xml: str, limit: int = 12) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for chunk in re.findall(r"<item>(.*?)</item>", xml, re.S)[:limit]:
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", chunk, re.S)
        link = re.search(r"<link>(.*?)</link>", chunk, re.S)
        published = re.search(r"<pubDate>(.*?)</pubDate>", chunk, re.S)
        source = re.search(r"<source[^>]*>(.*?)</source>", chunk, re.S)
        text = re.sub(r"<[^>]+>", "", title.group(1) if title else "")
        text = " ".join(text.split())
        if not text:
            continue
        stamp = None
        if published:
            try:
                stamp = int(datetime.strptime(published.group(1).strip(), "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                stamp = None
        items.append(
            {
                "title": text,
                "link": link.group(1).strip() if link else "",
                "publisher": re.sub(r"<[^>]+>", "", source.group(1)).strip() if source else "",
                "providerPublishTime": stamp,
            }
        )
    return items


def live_market_news() -> dict[str, Any]:
    request = Request(NEWS_FEED, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=8.0) as response:
        xml = response.read().decode("utf-8", "replace")
    return {"items": _rss_items(xml)}


WALLPAPER_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("^GSPC", "S&P 500"),
    ("^IXIC", "NASDAQ"),
    ("^DJI", "DOW 30"),
    ("^VIX", "VIX"),
    ("BTC-USD", "BITCOIN"),
    ("GC=F", "GOLD"),
    ("CL=F", "WTI CRUDE"),
    ("^TNX", "US 10Y"),
    ("DX-Y.NYB", "DOLLAR INDEX"),
    ("EURUSD=X", "EUR / USD"),
    ("^HSI", "HANG SENG"),
    ("^N225", "NIKKEI 225"),
)
WALLPAPER_LABELS = dict(WALLPAPER_SYMBOLS)
WALLPAPER_DEFAULT_HERO = "^GSPC"
WALLPAPER_CANDLES_TTL = 300

# CNBC is the primary feed for the wallpaper: one batched quote call for the
# whole screen and real intraday bars for every symbol (indices, futures, FX,
# crypto and yields), which the free Yahoo endpoints refuse under load.
WALLPAPER_CNBC: dict[str, str] = {
    "^GSPC": ".SPX",
    "^IXIC": ".IXIC",
    "^DJI": ".DJI",
    "^VIX": ".VIX",
    "^N225": ".N225",
    "^HSI": ".HSI",
    "^FTSE": ".FTSE",
    "BTC-USD": "BTC=",
    "GC=F": "@GC.1",
    "CL=F": "@CL.1",
    "^TNX": "US10Y",
    "DX-Y.NYB": ".DXY",
    "EURUSD=X": "EUR=",
    "USDJPY=X": "JPY=",
}
WALLPAPER_TIMEZONES: dict[str, str] = {
    "^N225": "Asia/Tokyo",
    "^HSI": "Asia/Hong_Kong",
    "^FTSE": "Europe/London",
}
WALLPAPER_SCALE_UNITS = {"^TNX": "%", "US10Y": "%", "GC=F": "USD", "CL=F": "USD"}
WALLPAPER_SESSION_WINDOWS: dict[str, tuple[str, str]] = {
    "^GSPC": ("09:30", "16:05"),
    "^IXIC": ("09:30", "16:05"),
    "^DJI": ("09:30", "16:05"),
    "^VIX": ("09:30", "16:20"),
    "^N225": ("09:00", "15:15"),
    "^HSI": ("09:30", "16:10"),
    "^FTSE": ("08:00", "16:35"),
}
# Which exchange clock each symbol follows. Futures / FX / crypto trade around
# the clock, so they are always "open" and sort first only when nothing else is.
WALLPAPER_SYMBOL_EXCHANGE: dict[str, str] = {
    "^GSPC": "nyse",
    "^IXIC": "nyse",
    "^DJI": "nyse",
    "^VIX": "nyse",
    "^FTSE": "lse",
    "^N225": "tse",
    "^HSI": "hkex",
}
WALLPAPER_CNBC_QUOTE_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
WALLPAPER_CNBC_CHART_URL = "https://ts-api.cnbc.com/harmony/app/charts/{range}.json"
WALLPAPER_NEW_YORK = ZoneInfo("America/New_York")


def _cnbc_number(value: Any) -> float | None:
    """CNBC sends "7,580.07", "+12.91%" and "UNCH"; parse what is real."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text.upper() in {"UNCH", "N/A", "NA", "NAN", "--"}:
        return None
    return safe_float(text)


def _wallpaper_downsample(points: list[list[float]], limit: int = 80) -> list[list[float]]:
    """Thin a session-filtered intraday series down to ``limit`` points.

    Stride sampling is right here: the series only contains traded time, so an
    even index step keeps each session equally dense. The browser collapses the
    closed-market gaps in between so the sessions sit shoulder to shoulder.
    """
    if len(points) <= limit:
        return points
    stride = max(1, math.ceil(len(points) / limit))
    thinned = [points[index] for index in range(0, len(points), stride)]
    if thinned[-1] is not points[-1]:
        thinned.append(points[-1])
    return thinned


def wallpaper_cnbc_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """One batched quote call for every symbol on the screen, cached 20 seconds."""
    pairs = [(symbol, WALLPAPER_CNBC.get(symbol)) for symbol in symbols]
    wanted = [cnbc for _, cnbc in pairs if cnbc]
    if not wanted:
        return {}

    def load() -> dict[str, dict[str, Any]]:
        url = (
            f"{WALLPAPER_CNBC_QUOTE_URL}?requestMethod=itv&noform=1&partnerId=2"
            f"&fund=1&exthrs=1&output=json&events=1&symbols={quote('|'.join(wanted), safe='')}"
        )
        payload = fetch_json_with_timeout(url, 8.0)
        rows = (payload.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("symbol") or "")
            if key and key != "INVALID_CHARACTER":
                found[key] = row
        return found

    try:
        raw = cached(("wallpaper_cnbc_quotes", tuple(wanted)), 20, load)
    except Exception:
        return {}
    return {symbol: raw[cnbc] for symbol, cnbc in pairs if cnbc and cnbc in raw}

WALLPAPER_EXCHANGES: tuple[dict[str, Any], ...] = (
    {
        "key": "nyse",
        "label": "NEW YORK",
        "tz": "America/New_York",
        "open": (9, 30),
        "close": (16, 0),
        "pre": (4, 0),
        "post": (20, 0),
    },
    {"key": "lse", "label": "LONDON", "tz": "Europe/London", "open": (8, 0), "close": (16, 30)},
    {"key": "tse", "label": "TOKYO", "tz": "Asia/Tokyo", "open": (9, 0), "close": (15, 0)},
    {"key": "hkex", "label": "HONG KONG", "tz": "Asia/Hong_Kong", "open": (9, 30), "close": (16, 0)},
)


def _clock_minutes(hour: int, minute: int) -> int:
    return hour * 60 + minute


def _local_moment(day: Any, hour: int, minute: int, tz: Any) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def _next_weekday_moment(local_now: datetime, hour: int, minute: int, tz: Any) -> datetime:
    candidate = _local_moment(local_now.date(), hour, minute, tz)
    while candidate <= local_now or candidate.weekday() >= 5:
        candidate = _local_moment(candidate.date() + timedelta(days=1), hour, minute, tz)
    return candidate


def exchange_session(exchange: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Open / pre / after / closed state plus the countdown to the next bell."""
    tz = ZoneInfo(str(exchange["tz"]))
    local = now.astimezone(tz)
    minutes = _clock_minutes(local.hour, local.minute)
    open_m = _clock_minutes(*exchange["open"])
    close_m = _clock_minutes(*exchange["close"])
    state = "closed"
    if local.weekday() < 5:
        pre = exchange.get("pre")
        post = exchange.get("post")
        if open_m <= minutes < close_m:
            state = "open"
        elif pre and _clock_minutes(*pre) <= minutes < open_m:
            state = "pre"
        elif post and close_m <= minutes < _clock_minutes(*post):
            state = "post"
    if state == "open":
        at = _local_moment(local.date(), exchange["close"][0], exchange["close"][1], tz)
        kind = "close"
    else:
        at = _next_weekday_moment(local, exchange["open"][0], exchange["open"][1], tz)
        kind = "open"
    labels = {"open": "OPEN", "pre": "PRE", "post": "AFTER", "closed": "CLOSED"}
    return {
        "key": exchange["key"],
        "label": exchange["label"],
        "state": state,
        "stateLabel": labels[state],
        "localTime": local.strftime("%H:%M"),
        "next": {
            "kind": kind,
            "at": at.isoformat(),
            "seconds": max(0, int((at - local).total_seconds())),
        },
    }


def wallpaper_sessions(now: datetime) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for exchange in WALLPAPER_EXCHANGES:
        try:
            sessions.append(exchange_session(exchange, now))
        except Exception:
            continue
    return sessions


def _wallpaper_spark(symbols: list[str], range_value: str, interval: str) -> dict[str, list[float]]:
    encoded = quote(",".join(symbols))
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={encoded}"
        f"&range={quote(range_value)}&interval={quote(interval)}"
    )
    payload = fetch_json_with_timeout(url, 6.0)
    series: dict[str, list[float]] = {}
    for key, item in (payload or {}).items():
        if not isinstance(item, dict):
            continue
        values = [value for value in (safe_float(v) for v in item.get("close") or []) if value is not None]
        if len(values) >= 2:
            series[normalize_symbol(str(item.get("symbol") or key)).upper()] = values
    return series


def _cnbc_points(symbol: str, range_value: str, interval: int) -> list[list[float]]:
    cnbc_symbol = WALLPAPER_CNBC.get(symbol)
    if not cnbc_symbol:
        return []
    url = (
        f"{WALLPAPER_CNBC_CHART_URL.format(range=range_value)}"
        f"?symbol={quote(cnbc_symbol, safe='')}&interval={interval}&requestMs=0"
    )
    payload = fetch_json_with_timeout(url, 8.0)
    bars = ((payload or {}).get("barData") or {}).get("priceBars") or []
    zones = {name: ZoneInfo(name) for name in set(WALLPAPER_TIMEZONES.values()) | {"America/New_York"}}
    window = WALLPAPER_SESSION_WINDOWS.get(symbol)
    zone = zones.get(WALLPAPER_TIMEZONES.get(symbol, "America/New_York"), WALLPAPER_NEW_YORK)
    points: list[list[float]] = []
    for bar in bars:
        close = _cnbc_number(bar.get("close"))
        stamp = str(bar.get("tradeTime") or "")
        if close is None or len(stamp) != 14 or not stamp.isdigit():
            continue
        try:
            moment = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=WALLPAPER_NEW_YORK)
        except ValueError:
            continue
        if window:
            # CNBC pads five-day charts with off-session prints; keep the regular
            # session only so a weekend never shows up as a flat line.
            local = moment.astimezone(zone)
            if local.weekday() >= 5:
                continue
            clock = local.strftime("%H:%M")
            if clock < window[0] or clock > window[1]:
                continue
        points.append([int(moment.timestamp()), close])
    return points


def _cnbc_rows(symbol: str, range_value: str, interval: int) -> list[dict[str, Any]]:
    """OHLC bars for one symbol, regular session only.

    Same filters as `_cnbc_points`, but keeps open/high/low so the page can draw
    candles with the very same data it uses for the line.
    """
    cnbc_symbol = WALLPAPER_CNBC.get(symbol)
    if not cnbc_symbol:
        return []
    url = (
        f"{WALLPAPER_CNBC_CHART_URL.format(range=range_value)}"
        f"?symbol={quote(cnbc_symbol, safe='')}&interval={interval}&requestMs=0"
    )
    payload = fetch_json_with_timeout(url, 8.0)
    bars = ((payload or {}).get("barData") or {}).get("priceBars") or []
    zones = {name: ZoneInfo(name) for name in set(WALLPAPER_TIMEZONES.values()) | {"America/New_York"}}
    window = WALLPAPER_SESSION_WINDOWS.get(symbol)
    zone = zones.get(WALLPAPER_TIMEZONES.get(symbol, "America/New_York"), WALLPAPER_NEW_YORK)
    rows: list[dict[str, Any]] = []
    for bar in bars:
        close = _cnbc_number(bar.get("close"))
        stamp = str(bar.get("tradeTime") or "")
        if close is None or len(stamp) != 14 or not stamp.isdigit():
            continue
        try:
            moment = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=WALLPAPER_NEW_YORK)
        except ValueError:
            continue
        if window:
            local = moment.astimezone(zone)
            if local.weekday() >= 5:
                continue
            clock = local.strftime("%H:%M")
            if clock < window[0] or clock > window[1]:
                continue
        rows.append(
            {
                "t": int(moment.timestamp()),
                "o": _cnbc_number(bar.get("open")) or close,
                "h": _cnbc_number(bar.get("high")) or close,
                "l": _cnbc_number(bar.get("low")) or close,
                "c": close,
            }
        )
    return rows


def wallpaper_candles(symbol: str, range_value: str = "5D", interval: int = 15, bars: int = 84) -> dict[str, Any]:
    """Candles for one symbol: 5D/15m by default, downsampled to `bars`."""

    def loader() -> dict[str, Any]:
        range_value_local = range_value if range_value in ("1D", "5D") else "5D"
        interval_local = 5 if range_value_local == "1D" else 15
        rows = _cnbc_rows(symbol, range_value_local, interval_local)
        if not rows and range_value_local != "5D":
            rows = _cnbc_rows(symbol, "5D", 15)
            range_value_local = "5D"
        stride = max(1, math.ceil(len(rows) / max(12, min(int(bars or 84), 240))))
        trimmed = rows[::stride]
        if rows and trimmed and trimmed[-1]["t"] != rows[-1]["t"]:
            trimmed.append(rows[-1])
        return {
            "symbol": symbol,
            "range": range_value_local,
            "interval": interval_local,
            "timezone": WALLPAPER_TIMEZONES.get(symbol, "America/New_York"),
            "bars": trimmed,
            "source": "cnbc" if trimmed else "none",
        }

    return cached(("wallpaper_candles", symbol, range_value, int(bars or 84)), WALLPAPER_CANDLES_TTL, loader)

def wallpaper_series(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Five days of intraday closes per symbol: CNBC, then Yahoo, else nothing."""

    def load_one(symbol: str) -> dict[str, Any]:
        for range_value, interval in (("5D", 15), ("1D", 5)):
            try:
                points = _wallpaper_downsample(_cnbc_points(symbol, range_value, interval))
            except Exception:
                points = []
            if len(points) >= 2:
                return {
                    "points": points,
                    "source": "cnbc",
                    "range": range_value,
                    "tz": WALLPAPER_TIMEZONES.get(symbol, "America/New_York"),
                }
        try:
            values = _wallpaper_spark([symbol], "5d", "30m").get(normalize_symbol(symbol).upper()) or []
        except Exception:
            values = []
        if len(values) >= 2:
            return {
                "points": [[float(index), value] for index, value in enumerate(values)],
                "source": "yahoo",
                "range": "5d",
                "tz": WALLPAPER_TIMEZONES.get(symbol, "America/New_York"),
            }
        return {"points": [], "source": "none", "range": "", "tz": ""}

    def load() -> dict[str, dict[str, Any]]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(load_one, symbols))
        return dict(zip(symbols, results))

    try:
        return cached(("wallpaper_series", tuple(symbols)), 600, load)
    except Exception:
        return {}


def wallpaper_symbol_session(symbol: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-symbol trading state so the layout can put open markets first."""
    key = WALLPAPER_SYMBOL_EXCHANGE.get(symbol)
    if not key:
        return {"key": "always", "label": "24H", "state": "open", "open": True, "nextSeconds": None}
    for session in sessions:
        if session.get("key") == key:
            body = session.get("next") or {}
            return {
                "key": key,
                "label": session.get("label") or key.upper(),
                "state": session.get("state") or "closed",
                "open": session.get("state") == "open",
                "stateLabel": session.get("stateLabel") or "CLOSED",
                "nextSeconds": body.get("seconds"),
                "nextKind": body.get("kind"),
            }
    return {"key": key, "label": key.upper(), "state": "closed", "open": False, "nextSeconds": None}


def wallpaper_quote_view(
    symbol: str,
    label: str,
    row: dict[str, Any],
    series: dict[str, dict[str, Any]],
    sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    price = _cnbc_number(row.get("last"))
    change = _cnbc_number(row.get("change"))
    change_pct = _cnbc_number(row.get("change_pct"))
    previous = _cnbc_number(row.get("previous_day_closing"))
    if price is not None and change is not None:
        derived = price - change
        # Some CNBC rows echo `last` into previous_day_closing; `change` is the
        # authoritative delta, so trust the derived close when they disagree.
        if previous is None or abs(previous - derived) > max(0.01, abs(derived) * 1e-4):
            previous = derived
    if change is None and price is not None and previous is not None:
        change = price - previous
    if change_pct is None and change is not None and previous:
        change_pct = change / previous * 100.0
    entry = series.get(symbol) or {}
    points = [list(point) for point in (entry.get("points") or [])]
    series_source = str(entry.get("source") or "none")
    if len(points) < 2 and price is not None:
        base = previous if previous is not None else price
        points = [[0.0, base], [1.0, price]]
        series_source = "flat"
    return {
        "symbol": symbol,
        "label": label or str(row.get("shortName") or row.get("name") or symbol),
        "name": str(row.get("name") or row.get("shortName") or ""),
        "price": price,
        "change": change,
        "changePct": change_pct,
        "previousClose": previous,
        "open": _cnbc_number(row.get("open")),
        "high": _cnbc_number(row.get("high")),
        "low": _cnbc_number(row.get("low")),
        "yearHigh": _cnbc_number(row.get("yrhiprice")),
        "yearLow": _cnbc_number(row.get("yrloprice")),
        "series": points,
        "seriesSource": series_source,
        "seriesRange": str(entry.get("range") or ""),
        "seriesTimezone": str(entry.get("tz") or ""),
        "marketStatus": str(row.get("curmktstatus") or ""),
        "asOf": str(row.get("last_time") or ""),
        "source": "cnbc" if row.get("last") else "",
        "currency": str(row.get("currencyCode") or "USD"),
        "session": wallpaper_symbol_session(symbol, sessions or []),
    }


def wallpaper_payload(symbols: list[str], hero_symbol: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    errors: list[str] = []
    row_map: dict[str, dict[str, Any]] = {}
    try:
        row_map.update(wallpaper_cnbc_quotes(symbols))
    except Exception as exc:
        errors.append(f"cnbc quotes: {exc}")
    missing = [symbol for symbol in symbols if not row_map.get(symbol, {}).get("last")]
    if missing:
        try:
            quote_payload = live_quote(",".join(missing), "auto")
        except Exception as exc:
            quote_payload = {}
            errors.append(f"quotes: {exc}")
        rows = (quote_payload.get("quoteResponse") or {}).get("result") or []
        for row in rows:
            key = normalize_symbol(str(row.get("symbol") or "")).upper()
            price = safe_float(row.get("regularMarketPrice"))
            if not key or price is None:
                continue
            change = safe_float(row.get("regularMarketChange"))
            row_map[key] = {
                "symbol": key,
                "last": price,
                "change": change,
                "change_pct": safe_float(row.get("regularMarketChangePercent")),
                "previous_day_closing": price - change if change is not None else None,
                "shortName": row.get("shortName") or row.get("longName") or "",
                "currencyCode": row.get("currency") or "USD",
            }
    series = wallpaper_series(symbols)
    sessions = wallpaper_sessions(now)
    quotes = [
        wallpaper_quote_view(symbol, WALLPAPER_LABELS.get(symbol, ""), row_map.get(symbol) or {}, series, sessions)
        for symbol in symbols
    ]
    hero = next((quote for quote in quotes if quote["symbol"] == hero_symbol), None) or wallpaper_quote_view(
        hero_symbol, WALLPAPER_LABELS.get(hero_symbol, ""), row_map.get(hero_symbol) or {}, series, sessions
    )

    sentiment = None
    try:
        reading = live_market_sentiment()
        gauges = reading.get("gauges") or []
        gauge = next((item for item in gauges if item.get("key") == "global"), gauges[0] if gauges else None)
        if gauge:
            sentiment = {
                "key": gauge.get("key"),
                "label": gauge.get("label") or "GLOBAL RISK",
                "score": safe_float(gauge.get("score")),
                "band": gauge.get("band") or "UNKNOWN",
                "detail": gauge.get("detail") or "",
            }
    except Exception as exc:
        errors.append(f"sentiment: {exc}")

    headlines: list[dict[str, Any]] = []
    try:
        news = live_market_news()
        for item in (news.get("items") or [])[:6]:
            title = " ".join(str(item.get("title") or "").split())
            if title:
                headlines.append(
                    {
                        "title": title,
                        "source": item.get("publisher") or item.get("source") or "",
                        "time": item.get("providerPublishTime"),
                        "link": item.get("link") or "",
                    }
                )
    except Exception as exc:
        errors.append(f"news: {exc}")

    return {
        "generatedAt": now.isoformat(),
        "hero": hero,
        "quotes": quotes,
        "sessions": wallpaper_sessions(now),
        "sentiment": sentiment,
        "headlines": headlines,
        "sources": sorted({str(quote.get("source") or "") for quote in quotes if quote.get("source")}),
        "errors": errors,
    }


def live_wallpaper(symbols_param: str = "", hero: str = "") -> dict[str, Any]:
    requested = [normalize_symbol(part.strip()) for part in (symbols_param or "").split(",") if part.strip()]
    symbols: list[str] = []
    for symbol in requested or [item[0] for item in WALLPAPER_SYMBOLS]:
        if symbol not in symbols:
            symbols.append(symbol)
    hero_symbol = normalize_symbol(hero.strip()) if hero.strip() else WALLPAPER_DEFAULT_HERO
    if hero_symbol not in symbols:
        symbols.insert(0, hero_symbol)
    symbols = symbols[:8]
    return cached(("wallpaper", tuple(symbols), hero_symbol), 30, lambda: wallpaper_payload(symbols, hero_symbol))

