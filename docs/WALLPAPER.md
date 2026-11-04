# Desk Tape — operator's guide

Deeper notes than the README: how the window is layered, what every setting does, and
how to debug a desktop that will not cooperate.

## 1. How the wallpaper is layered

| Layer | Window level | Owner |
| --- | --- | --- |
| System wallpaper | `-2147483623` (`kCGDesktopWindowLevel`) | WindowServer |
| **Desk Tape** | `-2147483610` | this app (click-through) |
| Finder desktop icons | `-2147483603` | Finder |
| Desktop widgets | `-2147483601` | WidgetKit |
| **Desk Tape while editing** | `-2147483600` | this app (mouse-enabled) |

The host builds one borderless `NSWindow` per `NSScreen`, with a `WKWebView` inside,
`canJoinAllSpaces`, `stationary`, `ignoresMouseEvents = True` and
`setCollectionBehavior` so it never appears in Mission Control as a normal window.
`scripts/wallpaper_status.py` asserts the level of the live window, which is the fastest
way to tell "installed" apart from "old process still running".

Because the window is click-through, the desktop stays fully usable: dragging files onto
icons, right-click menus and widget clicks all reach Finder. Only edit mode takes clicks,
and it turns itself off after 150 seconds.

## 2. Settings panel

Click the menu-bar icon → panel. Everything applies immediately.

- **Language** — 中文 / English; switches the whole page (`lang=zh|en`).
- **World clocks** — comma-separated IANA zones drawn in the clock strip and available
  to clock widgets.
- **Padding** — the safe inset around the screen (`inset=0,0,0,174` style), so nothing
  hides behind the Dock.
- **Priority** — `session` orders modules by the open market (Asia first when Asia is
  trading); `static` keeps the fixed order.
- **Chart style** — `K线 · TradingView` (default), `K线 · 原生`, `折线`.
- **Blocks / Widgets** — which modules exist; the widget strip has type + symbol pickers,
  `＋ 添加`, `− 删除`, `全部清除`, and shares the list with the page's edit bar.
- **Drag blocks with the mouse** — enters edit mode (same as pressing `L`).
- **Apply / Reload / Show wallpaper / Open in Browser / Quit**.

## 3. Layout file

`~/Library/Application Support/DeskTape/settings.json`:

```json
{
  "mode": "horizon",
  "hero": "^GSPC",
  "symbols": "^GSPC,^IXIC,^VIX,BTC-USD",
  "blocks": "ribbon,hero,chart,rail,tape,clocks,sessions,brand",
  "anchors": "hero:19.0,20.0,0.92;chart:0.0,70.0",
  "widgets": "[{\"id\":\"k1\",\"type\":\"candles\",\"symbol\":\"BTC-USD\"}]",
  "chart": "tv",
  "lang": "zh",
  "edit": false
}
```

`anchors` entries are `<block>:<x>%,<y>%[,<scale>]`; widget anchors use `w:<id>:…`.
Anchors for widgets that no longer exist are pruned on load, so the file cannot grow
stale entries.

## 4. Data pipeline

`desk_tape_server.py` → `wallpaper_data.py`:

1. **Quotes** — one batched CNBC call for every symbol (pipe-separated, 20 s cache).
2. **Series** — per symbol `5D/15m`, falling back to `1D/5m`, downsampled to ≤ 80 points
   (10 min cache). CNBC chart timestamps are New York wall-clock, converted to epoch.
3. **Candles** — `/api/wallpaper/candles?symbol=^GSPC&bars=84` returns OHLC bars for the
   native/tv renderers (5 min cache).
4. **Sessions** — four exchanges with open/close windows, `PRE`/`OPEN`/`CLOSED` states,
   local wall time and countdown to the next bell.
5. **Sentiment** — a transparent risk-appetite gauge derived from the quotes already
   fetched (S&P/NASDAQ/BTC moves and the VIX level); the page shows the score and band.
6. **Headlines** — Google News RSS, six items.

If a feed fails, the payload keeps the other sections and lists the fault in `errors`;
the page shows "—" instead of a fabricated number.

## 5. Debugging

```sh
tail -f ~/Library/Logs/DeskTape.log     # host: window levels, page URL, bridge payloads
python3.12 scripts/wallpaper_status.py  # level / click-through / process checks
python3.12 scripts/check_feeds.py       # per-symbol feed health
./scripts/wallpaper-reset.sh            # kill, rebuild, reinstall, relaunch
```

Common causes of "nothing on the desktop":

- an old process from a previous build still owns a window — `wallpaper-reset.sh`;
- the local server is down — the page shows a "waiting for the terminal" state and
  retries every 8 seconds; start it with `scripts/start.sh`;
- the app was opened from a copy with a stale `--url` — the panel's *Show wallpaper*
  rebuilds the windows from current settings.

## 6. Tests

```sh
python3.12 -m unittest discover -s tests -p "test_*.py"
```

The data tests cover number parsing (`UNCH`, `+1.2%`, commas), downsampling boundaries,
the quote-view contract, the honest two-point fallback, session shape and payload
assembly. Rendering itself is checked by screenshotting `http://127.0.0.1:8787/wallpaper`
in headless Chrome.
