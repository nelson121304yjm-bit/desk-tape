#!/usr/bin/env python3
"""Desk Tape host — pins the live market wallpaper behind your desktop icons.

One borderless WKWebView window per screen, sitting just above the wallpaper
picture and just below Finder icons. It never takes focus and never takes a
click: the desktop stays yours.

    python3 desktop/desk_tape.py --url http://127.0.0.1:8765 --mode horizon

Requires PyObjC (``pip install pyobjc-framework-Cocoa``); WebKit is loaded from
the system framework, so no extra package is needed for the web view.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

try:
    import objc  # noqa: F401
    from AppKit import (  # type: ignore
        NSAppleEventManager,
        NSApplication,
        NSButton,
        NSFloatingWindowLevel,
        NSFont,
        NSImage,
        NSMakeRect,
        NSPopUpButton,
        NSControl,
        NSTextField,
        NSTitledWindowMask,
        NSClosableWindowMask,
        NSApplicationActivationPolicyAccessory,
        NSApplicationDidChangeScreenParametersNotification,
        NSBackingStoreBuffered,
        NSBorderlessWindowMask,
        NSColor,
        NSEvent,
        NSMakePoint,
        NSControlStateValueOff,
        NSControlStateValueOn,
        NSMenu,
        NSMenuItem,
        NSOperationQueue,
        NSScreen,
        NSScrollView,
        NSStatusBar,
        NSVariableStatusItemLength,
        NSView,
        NSViewHeightSizable,
        NSViewWidthSizable,
        NSWindow,
        NSWorkspace,
    )
    from Foundation import (
        NSMakeSize,
        NSDate,
        NSNotificationCenter,
        NSRunLoop,
        NSObject,
        NSURL,
        NSURLRequest,
    )
except ImportError as exc:  # pragma: no cover - environment guard
    sys.exit(
        "Desk Tape needs PyObjC: pip install pyobjc-framework-Cocoa\n"
        f"({exc})"
    )

# WebKit ships with macOS but PyObjC does not bundle its bridge: load the framework.
objc.loadBundle("WebKit", globals(), "/System/Library/Frameworks/WebKit.framework")

# kCGDesktopIconWindowLevel is -20; one below keeps us under Finder icons and
# far above the wallpaper picture window.
# Layer values measured on this Mac with CGWindowListCopyWindowInfo:
#   system wallpaper ("墙纸")        -2147483625
#   WindowManager                    -2147483624
#   Finder desktop (icons/folders)   -2147483603
#   desktop widgets (通知中心)        -2147483601
# The window has to sit *above* the system wallpaper but *below* Finder, so
# folders and widgets keep drawing on top of it.
SYSTEM_WALLPAPER_LEVEL = -2147483625
FINDER_DESKTOP_LEVEL = -2147483603
WALLPAPER_LEVEL = SYSTEM_WALLPAPER_LEVEL + 15  # -2147483610, under Finder
# Editing has to receive clicks, so the window is lifted above Finder's desktop
# (icons at -2147483603, widgets at -2147483601) for the duration of the drag.
WALLPAPER_EDIT_LEVEL = FINDER_DESKTOP_LEVEL + 3  # -2147483600

# Post real OS-level mouse events (CGEvent -> WindowServer -> app hit test). This is
# the same path a physical mouse takes; only the source is synthetic.
_CG = None


def _cg():
    global _CG
    if _CG is not None:
        return _CG

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    lib.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    lib.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    lib.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    lib.CGEventPostToPid.argtypes = [ctypes.c_int, ctypes.c_void_p]
    _CG = (lib, CGPoint)
    return _CG


def post_mouse(kind, x, y, pid=None):
    lib, point = _cg()
    event = lib.CGEventCreateMouseEvent(None, kind, point(float(x), float(y)), 0)
    if not event:
        return False
    if pid:
        lib.CGEventPostToPid(int(pid), event)
    else:
        lib.CGEventPost(0, event)  # kCGHIDEventTap
    return True


MODES = (("horizon", "Horizon"), ("board", "Board"), ("focus", "Focus"), ("minimal", "Minimal"))

# Watchlist presets offered in the settings window.
PRESETS = (
    (
        "Classic 12",
        "^GSPC,^IXIC,^DJI,^VIX,^N225,^HSI,^FTSE,BTC-USD,GC=F,CL=F,^TNX,DX-Y.NYB",
    ),
    ("US indices", "^GSPC,^IXIC,^DJI,^VIX,^TNX,DX-Y.NYB"),
    ("Global macro", "^GSPC,^VIX,^TNX,DX-Y.NYB,EURUSD=X,GC=F,CL=F,^HSI,^N225,^FTSE"),
    ("Crypto + metals", "BTC-USD,ETH-USD,GC=F,CL=F,DX-Y.NYB,^VIX"),
    ("Asia session", "^HSI,^N225,^FTSE,^GSPC,^VIX,DX-Y.NYB,USDJPY=X"),
)

LOGIN_LABEL = "com.openmarket.desktape"
LOG_DIR = Path.home() / "Library" / "Logs"
LOG_PATH = LOG_DIR / "DeskTape.log"


LOG_LIMIT_BYTES = 512 * 1024


def log(message: str) -> None:
    """Small append-only trace; the app is LSUIElement, so stderr goes nowhere visible."""
    import datetime

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # One generation of history is enough; keep the file bounded without
        # touching the running app.
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_LIMIT_BYTES:
            LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".1"))
        # O_NOFOLLOW: never follow a planted symlink in a shared directory, and
        # 0600 so other local users cannot read the trace.
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def bundle_path() -> "Path | None":
    """The .app we are running from, so the menu can reveal it in Finder."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.suffix == ".app":
            return parent
    return None


def parse_inset(raw: str) -> dict[str, int]:
    """`60,320,40,0` or `left:220` → {top, right, bottom, left} in pixels."""
    out = {"top": 0, "right": 0, "bottom": 0, "left": 0}
    tokens = [token.strip() for token in (raw or "").split(",") if token.strip()]
    if len(tokens) == 4 and all(token.isdigit() for token in tokens):
        for name, token in zip(("top", "right", "bottom", "left"), tokens):
            out[name] = int(token)
        return out
    for token in tokens:
        name, _, value = token.partition(":")
        if name in out and value.isdigit():
            out[name] = int(value)
    return out


def _trim(url: str) -> str:
    trimmed = url.strip()
    while trimmed.endswith("/"):
        trimmed = trimmed[:-1]
    return trimmed


def settings_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "DeskTape" / "settings.json"


def load_settings() -> dict:
    import json

    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def store_settings(config) -> None:
    import json

    payload = {
        "mode": getattr(config, "mode", "horizon"),
        # Persist the base only: a full wallpaper URL would pin old params forever.
        "url": (getattr(config, "url", "") or "").split("?")[0],
        "rotate": int(getattr(config, "rotate", 0) or 0),
        "rotateModes": getattr(config, "rotate_modes", "") or "",
        "order": getattr(config, "order", "") or "",
        "symbols": getattr(config, "symbols", "") or "",
        "clocks": getattr(config, "clocks", "") or "",
        "inset": getattr(config, "inset", "") or "",
        "zoom": float(getattr(config, "zoom", 1.0) or 1.0),
        "align": getattr(config, "align", "left") or "left",
        "lang": getattr(config, "lang", "en") or "en",
        "blocks": getattr(config, "blocks", "") or "",
        "anchors": getattr(config, "anchors", "") or "",
        "priority": getattr(config, "priority", "default") or "default",
        "widgets": getattr(config, "widgets", "") or "",
        "chart": getattr(config, "chart", "tv") or "tv",
        "edit": bool(getattr(config, "edit", False)),
    }
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


WIDGET_KINDS = (
    ("quote", "报价"),
    ("spark", "走势"),
    ("candles", "K线"),
    ("clock", "时钟"),
    ("gauge", "情绪"),
    ("watch", "自选"),
)
WIDGET_LIMIT = 8
WIDGET_SYMBOL_KINDS = {"quote", "spark", "candles", "watch"}
DEFAULT_WIDGET_SYMBOLS = ("^GSPC", "^IXIC", "^VIX", "BTC-USD", "GC=F", "CL=F")


def load_widgets(config) -> list[dict]:
    """Normalise the `widgets` setting into a list of usable widget dicts."""
    raw = getattr(config, "widgets", "") or ""
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    known = {kind for kind, _label in WIDGET_KINDS}
    out = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in known:
            continue
        out.append(
            {
                "id": str(item.get("id") or ""),
                "type": str(item["type"]),
                "symbol": str(item.get("symbol") or "").strip().upper(),
                "zone": str(item.get("zone") or ""),
            }
        )
    return out[:WIDGET_LIMIT]


def store_widgets(config, items) -> None:
    config.widgets = json.dumps(items, separators=(",", ":")) if items else ""


def drop_widget_anchors(config, keep_ids=None) -> None:
    """Remove saved positions that no longer have a widget behind them."""
    keep = set(keep_ids) if keep_ids is not None else set()
    parts = []
    for part in (getattr(config, "anchors", "") or "").split(";"):
        if not part:
            continue
        # Widget anchors read "w:<id>:x,y", so the id ends at the next colon.
        if part.startswith("w:") and part[2:].split(":", 1)[0] not in keep:
            continue
        parts.append(part)
    config.anchors = ";".join(parts)


def add_widget(config, kind, symbol="") -> tuple[bool, str]:
    """Append one widget, returning (added, message for the panel)."""
    labels = dict(WIDGET_KINDS)
    kind = kind if kind in labels else "quote"
    symbol = (symbol or "").strip().upper()
    items = load_widgets(config)
    if len(items) >= WIDGET_LIMIT:
        return False, f"最多 {WIDGET_LIMIT} 个组件"
    if kind in WIDGET_SYMBOL_KINDS:
        if not symbol:
            return False, "先选一个标的"
        if any(item["type"] == kind and item["symbol"] == symbol for item in items):
            return False, f"已有 {labels[kind]} · {symbol}"
    used = {item["id"] for item in items}
    index = 1
    while f"{kind}{index}" in used:
        index += 1
    items.append(
        {
            "id": f"{kind}{index}",
            "type": kind,
            "symbol": symbol,
            "zone": "Asia/Shanghai" if kind == "clock" else "",
        }
    )
    store_widgets(config, items)
    return True, f"已添加 {labels[kind]}" + (f" · {symbol}" if symbol else "")


def remove_widget(config, widget_id) -> bool:
    items = load_widgets(config)
    kept = [item for item in items if item["id"] != widget_id]
    if len(kept) == len(items):
        return False
    store_widgets(config, kept)
    drop_widget_anchors(config, [item["id"] for item in kept])
    return True


SETTING_FLAGS = {
    "mode": "--mode",
    "rotate": "--rotate",
    "rotateModes": "--rotate-modes",
    "symbols": "--symbols",
    "order": "--order",
    "clocks": "--clocks",
    "inset": "--inset",
    "zoom": "--zoom",
    "align": "--align",
    "lang": "--lang",
    "url": "--url",
    "widgets": "--widgets",
    "chart": "--chart",
}


def apply_defaults(config):
    """A Finder/LaunchAgent launch carries no arguments: read the saved file.

    Explicit CLI flags win over the stored file, so `--args --lang zh` is a
    reproducible way to test a layout without editing settings.
    """
    argv = sys.argv[1:]
    saved = load_settings()
    if saved.get("mode") in {name for name, _ in MODES} and "--mode" not in argv:
        config.mode = saved["mode"]
    if saved.get("url") and "--url" not in argv:
        config.url = saved["url"]
    if str(saved.get("rotate", "")).isdigit() and "--rotate" not in argv:
        config.rotate = int(saved["rotate"])
    for key in (
        "rotateModes",
        "order",
        "symbols",
        "clocks",
        "inset",
        "blocks",
        "anchors",
        "priority",
        "widgets",
        "chart",
    ):
        if SETTING_FLAGS.get(key) in argv:
            continue
        value = saved.get(key) or ""
        if value:
            setattr(config, "rotate_modes" if key == "rotateModes" else key, value)
    if isinstance(saved.get("zoom"), (int, float)) and SETTING_FLAGS["zoom"] not in argv:
        config.zoom = float(saved["zoom"])
    if saved.get("align") in ("left", "center", "right") and SETTING_FLAGS["align"] not in argv:
        config.align = saved["align"]
    if saved.get("lang") in ("en", "zh") and SETTING_FLAGS["lang"] not in argv:
        config.lang = saved["lang"]
    if "--edit" not in argv:
        config.edit = bool(saved.get("edit", False))
    return config


def save_defaults(config) -> None:
    store_settings(config)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def safe_page_url(raw: str) -> str:
    """Only http(s) URLs the wallpaper can actually render are allowed through.

    A config-supplied URL is loaded into a WKWebView that owns the DeskTapePost
    bridge, so a file: or remote page must never be accepted silently.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    hostname = (parsed.hostname or "").lower()
    allow_remote = os.environ.get("DESKTAPE_ALLOW_REMOTE_URL", "").strip() in ("1", "true", "yes")
    if not allow_remote and hostname not in LOOPBACK_HOSTS:
        return ""
    return raw


def page_url(config: "Config") -> NSURL | None:
    base = _trim(config.url)
    # A URL that already carries a query is used verbatim; a bare page URL still
    # needs the configuration appended, otherwise mode/lang/edit never reach it.
    if "wallpaper" in base and "?" in base:
        safe = safe_page_url(base)
        return NSURL.URLWithString_(safe) if safe else None
    from urllib.parse import urlencode

    query = {"mode": config.mode, "refresh": str(config.refresh), "hud": "0"}
    if config.hero:
        query["hero"] = config.hero
    if config.symbols:
        query["symbols"] = config.symbols
    if config.lang == "zh":
        query["lang"] = "zh"
    rotate_modes = getattr(config, "rotate_modes", "")
    if rotate_modes:
        query["rotate"] = rotate_modes
    elif getattr(config, "rotate", 0):
        query["rotate"] = str(config.rotate)
    if getattr(config, "order", ""):
        query["order"] = config.order
    if getattr(config, "clocks", ""):
        query["clocks"] = config.clocks
    if getattr(config, "inset", ""):
        query["inset"] = config.inset
    if float(getattr(config, "zoom", 1.0) or 1.0) != 1.0:
        query["zoom"] = f"{float(config.zoom):.2f}".rstrip("0").rstrip(".")
    if getattr(config, "align", "left") != "left":
        query["align"] = config.align
    if getattr(config, "blocks", ""):
        query["blocks"] = config.blocks
    if getattr(config, "anchors", ""):
        query["anchors"] = config.anchors
    if getattr(config, "chart", "line") in ("candles", "tv"):
        query["chart"] = config.chart
    if getattr(config, "widgets", ""):
        query["widgets"] = config.widgets
    if getattr(config, "priority", "default") != "default":
        query["priority"] = config.priority
    if getattr(config, "edit", False):
        query["edit"] = "1"
    if getattr(config, "probe", False):
        query["probe"] = "1"
    root = base.rstrip("/")
    if root.endswith("/wallpaper"):
        root = root[: -len("/wallpaper")]
    return NSURL.URLWithString_(f"{root}/wallpaper?{urlencode(query)}")


OFFLINE_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#05070a;color:#f4f1e8;
  font:12px/1.6 ui-monospace,Menlo,monospace;letter-spacing:.18em}
.wrap{position:absolute;left:6vw;top:50%;transform:translateY(-50%)}
.bar{width:28px;height:3px;background:#ffb000;margin-bottom:18px}
h1{font:500 28px/1.2 "Helvetica Neue",sans-serif;letter-spacing:-.01em;margin:0 0 10px}
p{color:#8b9498;margin:0 0 6px}
code{color:#ffb000}
</style></head><body><div class="wrap">
<div class="bar"></div>
<h1>Desk Tape is waiting for the terminal</h1>
<p>Start the server: <code>python3.12 server.py</code></p>
<p>Then open <code>http://127.0.0.1:8765/wallpaper</code></p>
</div></body></html>"""


class TapeWindow(NSWindow):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class TapeDelegate(NSObject):
    """Retries the page politely when the terminal server is not up yet."""

    def initWithScreen_config_(self, screen, config):
        self = objc.super(TapeDelegate, self).init()
        if self is None:
            return None
        self.url = page_url(config)
        self.retry = None
        return self

    def webView_didFinishNavigation_(self, webview, navigation):
        if self.retry is not None:
            self.retry.invalidate()
            self.retry = None

    def webView_didFailProvisionalNavigation_withError_(self, webview, navigation, error):
        self.showOfflinePage_(webview)

    def webView_didFailNavigation_withError_(self, webview, navigation, error):
        self.showOfflinePage_(webview)

    def showOfflinePage_(self, webview):
        webview.loadHTMLString_baseURL_(OFFLINE_HTML, None)
        if self.retry is None and self.url is not None:
            from AppKit import NSTimer

            self.retry = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                8.0,
                True,
                lambda _timer: webview.loadRequest_(NSURLRequest.requestWithURL_(self.url)),
            )


class LayoutBridge(NSObject):
    """Receives `window.webkit.messageHandlers.desktape.postMessage(layout)`."""

    owner = objc.ivar()

    def userContentController_didReceiveScriptMessage_(self, _controller, message):
        owner = self.owner
        if owner is None:
            return
        body = message.body()
        try:
            if isinstance(body, dict) and body.get("probe"):
                owner.probe_events.append(body)
                log("probe %s at %s,%s" % (body.get("probe"), body.get("x"), body.get("y")))
                return
            owner.applyLayoutPayload(body)
        except Exception as exc:  # noqa: BLE001
            log("layout bridge failed: %s" % exc)


class Controller(NSObject):
    def initWithConfig_(self, config):
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        self.config = config
        self.windows = []
        self.bridges = []
        self.status_item = None
        self.settings_window = None
        self.candle_box = None
        self.wallpaper_visible = True
        self.settings_fields = {}
        return self

    # ---------------------------------------------------------------- screens
    def buildScreens_(self, _notification=None):
        screens = list(NSScreen.screens())
        self._screen_signature = self.screenSignature(screens)
        for window, _delegate in self.windows:
            view = window.contentView()
            view.stopLoading()
            view.setNavigationDelegate_(None)
            view.configuration().userContentController().removeScriptMessageHandlerForName_("desktape")
            window.orderOut_(None)
            window.close()
        for bridge in self.bridges:
            bridge.owner = None
        self.bridges = []
        self.windows = []
        for screen in screens:
            frame = screen.frame()
            window = TapeWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(
                frame, NSBorderlessWindowMask, NSBackingStoreBuffered, False, screen
            )
            window.setOpaque_(True)
            window.setBackgroundColor_(NSColor.blackColor())
            window.setHasShadow_(False)
            editing = bool(getattr(self.config, "edit", False))
            interactive = bool(getattr(self.config, "interactive", False) or editing)
            window.setIgnoresMouseEvents_(not interactive)
            window.setAcceptsMouseMovedEvents_(interactive)
            level = WALLPAPER_EDIT_LEVEL if editing else WALLPAPER_LEVEL
            window.setReleasedWhenClosed_(False)
            alpha = self.config.opacity
            if editing:
                # Non-opaque + transparent page: the desktop shows through while the
                # modules float above it, like arranging widgets.
                window.setOpaque_(False)
                window.setBackgroundColor_(NSColor.clearColor())
                alpha = 1.0
            else:
                window.setOpaque_(True)
                window.setBackgroundColor_(NSColor.blackColor())
            window.setAlphaValue_(alpha)
            window.setLevel_(level)
            log(
                "screen window: level=%s editing=%s finder_desktop=%s screen=%.0fx%.0f"
                % (level, editing, FINDER_DESKTOP_LEVEL, frame.size.width, frame.size.height)
            )
            window.setCollectionBehavior_(
                1 | 16 | 512 | 128  # canJoinAllSpaces | stationary | ignoresCycle | fullScreenNone
            )

            configuration = WKWebViewConfiguration.alloc().init()
            # Private store: no history, cookies or cache on disk. The layout lives in
            # settings.json and comes back to the page through the URL.
            configuration.setWebsiteDataStore_(WKWebsiteDataStore.nonPersistentDataStore())
            controller = WKUserContentController.alloc().init()
            bridge = LayoutBridge.alloc().init()
            bridge.owner = self
            controller.addScriptMessageHandler_name_(bridge, "desktape")
            configuration.setUserContentController_(controller)
            self.bridges.append(bridge)
            configuration.setSuppressesIncrementalRendering_(False)
            webview = WKWebView.alloc().initWithFrame_configuration_(frame, configuration)
            webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
            webview.setWantsLayer_(True)
            webview.setOpaque_(False)
            webview.setValue_forKey_(False, "drawsBackground")
            delegate = TapeDelegate.alloc().initWithScreen_config_(screen, self.config)
            webview.setNavigationDelegate_(delegate)
            window.setContentView_(webview)
            window.orderFrontRegardless()
            if delegate.url is not None:
                webview.loadRequest_(NSURLRequest.requestWithURL_(delegate.url))
            self.windows.append((window, delegate))

    @objc.python_method
    def screenSignature(self, screens=None):
        return tuple(
            (str(screen.deviceDescription().get("NSScreenNumber")),
             float(screen.frame().origin.x), float(screen.frame().origin.y),
             float(screen.frame().size.width), float(screen.frame().size.height),
             float(screen.backingScaleFactor()))
            for screen in (screens if screens is not None else NSScreen.screens())
        )

    def screensChanged_(self, _notification):
        # Menu-bar/activation changes also emit this notification. Only rebuild
        # when display identity, geometry or backing scale actually changed.
        if self.screenSignature() != getattr(self, "_screen_signature", None):
            self.buildScreens_(None)

    # ------------------------------------------------------------------ menu
    def buildMenu(self):
        """One menu-bar icon; clicking it opens the settings panel."""
        item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        button = item.button()
        button.setToolTip_("Desk Tape — click to adjust the wallpaper")
        icon = self.statusIcon()
        if icon is not None:
            button.setImage_(icon)
            button.setImagePosition_(1)  # NSImageOnly
            button.setTitle_("")
        else:
            button.setTitle_("DT")
        button.setTarget_(self)
        button.setAction_("statusClicked:")
        self.status_item = item
        size = button.image().size() if button.image() else (0, 0)
        log(
            "status item: image=%.0fx%.0f title=%r frame=%.0fx%.0f"
            % (size[0], size[1], button.title(), button.frame().size.width, button.frame().size.height)
        )
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            2.0, False, lambda _timer=None: self.logStatusItemState()
        )

    @objc.python_method
    def statusIcon(self):
        """A real, sized image: SF Symbol first, bundle icns second."""
        symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "chart.line.uptrend.xyaxis", "Desk Tape"
        )
        if symbol is not None:
            config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(13.0, 0.0)
            configured = symbol.imageWithSymbolConfiguration_(config) if config is not None else None
            image = configured or symbol
            image.setTemplate_(True)
            image.setSize_(NSMakeSize(18.0, 16.0))
            return image
        base = bundle_path()
        if base is not None:
            path = base / "Contents" / "Resources" / "AppIcon.icns"
            if path.exists():
                image = NSImage.alloc().initWithContentsOfFile_(str(path))
                if image is not None:
                    image.setSize_(NSMakeSize(18.0, 18.0))
                    return image
        return None

    @objc.python_method
    def logStatusItemState(self):
        item = getattr(self, "status_item", None)
        if item is None:
            log("status item: missing")
            return
        button = item.button()
        window = button.window() if button is not None else None
        log(
            "status item live: visible=%s window=%s frame=%s"
            % (
                bool(item.isVisible()),
                bool(window.isVisible()) if window is not None else "no-window",
                button.frame() if button is not None else "no-button",
            )
        )

    def statusClicked_(self, _sender):
        self.openSettings_(None)

    # -------------------------------------------------------------- settings
    @objc.python_method
    def _label(self, view, x, y, text, size=10.5, dim=True):
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, 200, 16))
        field.setStringValue_(text)
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(False)
        field.setSelectable_(False)
        field.setFont_(NSFont.systemFontOfSize_(size))
        if dim:
            field.setTextColor_(NSColor.secondaryLabelColor())
        view.addSubview_(field)
        return field

    @objc.python_method
    def _buildWidgetSection(self, view, base, width):
        """Widget manager: add and delete widgets without opening the edit bar."""
        self._label(view, 16, base + 100, "组件 · 与壁纸编辑栏共用同一份列表")
        self.widget_count = self._label(view, 246, base + 100, "")
        self.widget_count.setFrame_(NSMakeRect(246, base + 100, 208, 16))

        self.widget_type = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(16, base + 64, 108, 24), False
        )
        self.widget_type.addItemsWithTitles_([label for _kind, label in WIDGET_KINDS])
        view.addSubview_(self.widget_type)

        saved_symbols = [
            token.strip() for token in (getattr(self.config, "symbols", "") or "").split(",") if token.strip()
        ]
        self.widget_symbols = saved_symbols or list(DEFAULT_WIDGET_SYMBOLS)
        self.widget_symbol = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(130, base + 64, 140, 24), False
        )
        self.widget_symbol.addItemsWithTitles_(self.widget_symbols)
        view.addSubview_(self.widget_symbol)

        add = NSButton.alloc().initWithFrame_(NSMakeRect(276, base + 62, 86, 26))
        add.setTitle_("＋ 添加")
        add.setBezelStyle_(1)
        add.setTarget_(self)
        add.setAction_("addWidgetFromPanel:")
        view.addSubview_(add)

        self.widget_list = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(16, base + 28, 250, 26), False
        )
        view.addSubview_(self.widget_list)

        remove = NSButton.alloc().initWithFrame_(NSMakeRect(272, base + 28, 82, 26))
        remove.setTitle_("− 删除")
        remove.setBezelStyle_(1)
        remove.setTarget_(self)
        remove.setAction_("removeWidgetFromPanel:")
        view.addSubview_(remove)

        clear = NSButton.alloc().initWithFrame_(NSMakeRect(360, base + 28, 96, 26))
        clear.setTitle_("全部清除")
        clear.setBezelStyle_(1)
        clear.setTarget_(self)
        clear.setAction_("clearWidgets:")
        view.addSubview_(clear)

        self.widget_note = self._label(view, 16, base + 6, "")
        self.widget_note.setFrame_(NSMakeRect(16, base + 6, 438, 16))

    @objc.python_method
    def refreshWidgetControls(self):
        if getattr(self, "widget_list", None) is None:
            return
        labels = dict(WIDGET_KINDS)
        items = load_widgets(self.config)
        menu = self.widget_list.menu()
        menu.removeAllItems()
        for item in items:
            title = labels.get(item["type"], item["type"])
            if item.get("symbol"):
                title = f"{title} · {item['symbol']}"
            menu.addItemWithTitle_action_keyEquivalent_(title, None, "")
        self.widget_list.setEnabled_(bool(items))
        if items:
            self.widget_list.selectItemAtIndex_(0)
        if getattr(self, "widget_count", None) is not None:
            self.widget_count.setStringValue_(f"{len(items)}/{WIDGET_LIMIT} 个")
        if getattr(self, "widget_note", None) is not None and not self.widget_note.stringValue():
            self.widget_note.setStringValue_("选中后点 − 删除，改动立即生效")

    def addWidgetFromPanel_(self, _sender=None):
        kinds = [kind for kind, _label in WIDGET_KINDS]
        index = self.widget_type.indexOfSelectedItem()
        kind = kinds[index] if 0 <= index < len(kinds) else "quote"
        symbol_index = self.widget_symbol.indexOfSelectedItem()
        symbol = self.widget_symbols[symbol_index] if 0 <= symbol_index < len(self.widget_symbols) else ""
        ok, message = add_widget(self.config, kind, symbol)
        self.widget_note.setStringValue_(message)
        if not ok:
            return
        save_defaults(self.config)
        self.refreshWidgetControls()
        self.buildScreens_(None)
        log("widget added from panel: %s %s" % (kind, symbol))

    def removeWidgetFromPanel_(self, _sender=None):
        items = load_widgets(self.config)
        index = self.widget_list.indexOfSelectedItem()
        if not (0 <= index < len(items)):
            self.widget_note.setStringValue_("先选一个组件再删除")
            return
        remove_widget(self.config, items[index]["id"])
        self.widget_note.setStringValue_("已删除")
        save_defaults(self.config)
        self.refreshWidgetControls()
        self.buildScreens_(None)
        log("widget removed from panel: %s" % (items[index]["id"],))

    def openSettings_(self, _sender=None):
        try:
            if self.settings_window is None:
                self._buildSettingsWindow()
            self.refreshWidgetControls()
            window = self.settings_window
            window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            frame = window.frame()
            log(
                "settings opened: visible=%s windowNumber=%s frame=%.0fx%.0f level=%s"
                % (
                    window.isVisible(),
                    window.windowNumber(),
                    frame.size.width,
                    frame.size.height,
                    window.level(),
                )
            )
        except Exception as error:  # noqa: BLE001 - surfaced through the trace log
            log("settings failed: %r" % (error,))

    @objc.python_method
    def _buildSettingsWindow(self):
        width, height = 470, 760  # classic grid, still measured from the bottom
        strip_height = 132  # widget manager sits above the classic grid
        document_height = height + strip_height
        # Fit the shell to the display; the scroll view only engages on short screens.
        available = NSScreen.mainScreen().visibleFrame().size.height
        window_height = int(min(document_height, available - 40))
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, window_height),
            NSTitledWindowMask | NSClosableWindowMask,
            NSBackingStoreBuffered,
            False,
        )
        # The controller reuses this window after the red close button. AppKit
        # must not release the native object while its Python proxy is retained.
        window.setReleasedWhenClosed_(False)
        window.setTitle_("Desk Tape — Settings")
        window.setLevel_(NSFloatingWindowLevel)
        window.center()

        content = window.contentView()
        scroll = NSScrollView.alloc().initWithFrame_(content.bounds())
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        scroll.setBorderType_(0)
        document = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, document_height))
        scroll.setDocumentView_(document)
        content.addSubview_(scroll)
        self.settings_scroll = scroll
        view = document
        self._buildWidgetSection(view, height, width)

        def field(x, y, w, value, placeholder=""):
            node = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 24))
            node.setStringValue_(value)
            node.setPlaceholderString_(placeholder)
            node.setFont_(NSFont.userFixedPitchFontOfSize_(11.5))
            view.addSubview_(node)
            return node

        # Watchlist
        self._label(view, 16, height - 34, "WATCHLIST · comma separated symbols")
        symbols = field(16, height - 62, 300, getattr(self.config, "symbols", ""), "^GSPC,^IXIC,^VIX")
        preset = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(324, height - 62, 130, 24), False)
        preset.addItemsWithTitles_([name for name, _ in PRESETS])
        preset.setTarget_(self)
        preset.setAction_("applyPreset:")
        view.addSubview_(preset)

        # Priority order
        self._label(view, 16, height - 96, "PRIORITY ORDER · drives auto-switch sequence")
        order = field(16, height - 124, 438, getattr(self.config, "order", ""), "^GSPC,^IXIC,^DJI,^VIX")

        # Layout + auto-switch
        self._label(view, 16, height - 158, "LAYOUT")
        mode = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(16, height - 186, 150, 24), False)
        mode.addItemsWithTitles_([label for _, label in MODES])
        names = [name for name, _ in MODES]
        if self.config.mode in names:
            mode.selectItemAtIndex_(names.index(self.config.mode))
        view.addSubview_(mode)

        self._label(view, 196, height - 158, "AUTO-SWITCH")
        rotate = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(196, height - 186, 110, 24), False)
        rotate.addItemsWithTitles_([f"{seconds}s" if seconds else "Off" for seconds in self.rotate_values])
        for index, seconds in enumerate(self.rotate_values):
            rotate.itemArray()[index].setRepresentedObject_(str(seconds))
        current = str(getattr(self.config, "rotate", 0))
        for index, seconds in enumerate(self.rotate_values):
            if str(seconds) == current:
                rotate.selectItemAtIndex_(index)
        rotate.setTarget_(self)
        rotate.setAction_("applySettings:")
        view.addSubview_(rotate)

        mode.setTarget_(self)
        mode.setAction_("applySettings:")

        note = self._label(
            view,
            16,
            height - 214,
            "Pick a preset or type your own list. Changes reload the wallpaper at once.",
        )

        # World clocks
        self._label(view, 16, height - 250, "WORLD CLOCKS · IANA zones, comma separated")
        clocks = field(
            16,
            height - 278,
            438,
            getattr(self.config, "clocks", ""),
            "America/New_York, Europe/London, Asia/Shanghai, Asia/Tokyo",
        )

        # Layout: insets + scale + alignment
        self._label(view, 16, height - 312, "INSETS · top,right,bottom,left in px")
        inset = field(
            16,
            height - 340,
            200,
            getattr(self.config, "inset", ""),
            "60,320,40,0",
        )
        zoom = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(232, height - 340, 90, 24), False)
        zoom.addItemsWithTitles_(["80%", "90%", "100%", "110%", "120%"])
        zoom_values = (0.8, 0.9, 1.0, 1.1, 1.2)
        current_zoom = float(getattr(self.config, "zoom", 1.0) or 1.0)
        nearest = min(range(len(zoom_values)), key=lambda i: abs(zoom_values[i] - current_zoom))
        zoom.selectItemAtIndex_(nearest)
        zoom.setTarget_(self)
        zoom.setAction_("applySettings:")
        view.addSubview_(zoom)

        align = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(334, height - 340, 120, 24), False)
        align.addItemsWithTitles_(["Align left", "Align center", "Align right"])
        align_names = ["left", "center", "right"]
        if getattr(self.config, "align", "left") in align_names:
            align.selectItemAtIndex_(align_names.index(self.config.align))
        align.setTarget_(self)
        align.setAction_("applySettings:")
        view.addSubview_(align)

        # Language
        self._label(view, 16, height - 374, "LANGUAGE")
        lang = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(16, height - 402, 130, 24), False)
        lang.addItemsWithTitles_(["English", "中文"])
        lang.selectItemAtIndex_(1 if getattr(self.config, "lang", "en") == "zh" else 0)
        lang.setTarget_(self)
        lang.setAction_("applySettings:")
        view.addSubview_(lang)

        # Layout sliders: the explicit way to nudge the design away from icons.
        insets = parse_inset(getattr(self.config, "inset", ""))
        self._label(view, 16, 330, "PADDING · pixels from each screen edge")
        self.inset_sliders = {}
        self.inset_values = {}
        slider_rows = (
            ("top", 16, 302),
            ("right", 246, 302),
            ("bottom", 16, 272),
            ("left", 246, 272),
        )
        for name, x, y in slider_rows:
            slider = NSSlider.alloc().initWithFrame_(NSMakeRect(x, y, 130, 20))
            slider.setMinValue_(0)
            slider.setMaxValue_(400)
            slider.setIntValue_(int(insets.get(name, 0)))
            slider.setTarget_(self)
            slider.setAction_("applySettings:")
            slider.setContinuous_(False)
            view.addSubview_(slider)
            self.inset_sliders[name] = slider
            tag = NSTextField.labelWithString_(f"{name.upper()} {int(insets.get(name, 0))}px")
            tag.setFrame_(NSMakeRect(x + 134, y - 2, 92, 18))
            tag.setFont_(NSFont.systemFontOfSize_(10))
            tag.setTextColor_(NSColor.secondaryLabelColor())
            view.addSubview_(tag)
            self.inset_values[name] = tag

        # Ordering: which symbols lead the lists.
        self._label(view, 16, 240, "PRIORITY")
        priority = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(16, 212, 150, 24), False)
        priority.addItemsWithTitles_(["Payload order", "Open markets first", "My order only"])
        priority_names = ["default", "session", "order"]
        if getattr(self.config, "priority", "default") in priority_names:
            priority.selectItemAtIndex_(priority_names.index(self.config.priority))
        priority.setTarget_(self)
        priority.setAction_("applySettings:")
        view.addSubview_(priority)

        self._label(view, 190, 240, "BLOCKS · empty = everything")
        blocks = field(190, 212, 264, getattr(self.config, "blocks", ""), "hero,chart,rail,tape")

        chart = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(246, 174, 208, 26), False)
        chart.addItemsWithTitles_(["K线 · TradingView", "K线 · 原生", "折线"])
        chart_names = ["tv", "candles", "line"]
        if getattr(self.config, "chart", "tv") in chart_names:
            chart.selectItemAtIndex_(chart_names.index(self.config.chart))
        chart.setTarget_(self)
        chart.setAction_("applySettings:")
        view.addSubview_(chart)

        save = NSButton.alloc().initWithFrame_(NSMakeRect(16, 174, 220, 26))
        save.setTitle_("Save dragged layout")
        save.setBezelStyle_(1)
        save.setTarget_(self)
        save.setAction_("saveLayout:")
        view.addSubview_(save)

        login_box = NSButton.alloc().initWithFrame_(NSMakeRect(16, 62, 170, 24))
        login_box.setButtonType_(3)  # NSSwitchButton
        login_box.setTitle_("Start at login")
        login_box.setState_(NSControlStateValueOn if login_installed() else NSControlStateValueOff)
        login_box.setTarget_(self)
        login_box.setAction_("toggleLogin:")
        view.addSubview_(login_box)

        drag_box = NSButton.alloc().initWithFrame_(NSMakeRect(16, 116, 220, 24))
        drag_box.setButtonType_(3)  # NSSwitchButton
        drag_box.setTitle_("拖动布局（进入编辑模式）")
        drag_box.setState_(NSControlStateValueOn if getattr(self.config, "edit", False) else NSControlStateValueOff)
        drag_box.setTarget_(self)
        drag_box.setAction_("toggleEdit:")
        view.addSubview_(drag_box)
        self.drag_box = drag_box

        show_box = NSButton.alloc().initWithFrame_(NSMakeRect(196, 88, 170, 24))
        show_box.setButtonType_(3)  # NSSwitchButton
        show_box.setTitle_("Show wallpaper")
        show_box.setState_(NSControlStateValueOn if self.wallpaper_visible else NSControlStateValueOff)
        show_box.setTarget_(self)
        show_box.setAction_("toggleWallpaperVisibility:")
        view.addSubview_(show_box)
        self.show_box = show_box

        browser_button = NSButton.alloc().initWithFrame_(NSMakeRect(210, 60, 140, 28))
        browser_button.setTitle_("Open in Browser")
        browser_button.setBezelStyle_(1)
        browser_button.setTarget_(self)
        browser_button.setAction_("openTerminal:")
        view.addSubview_(browser_button)

        for title, action, x, w in (
            ("Apply", "applySettings:", 16, 100),
            ("Reload", "reload:", 120, 86),
            ("Quit", "quit:", 210, 80),
            ("Close", "closeSettings:", 294, 86),
        ):
            node = NSButton.alloc().initWithFrame_(NSMakeRect(x, 18, w, 30))
            node.setTitle_(title)
            node.setBezelStyle_(1)
            node.setTarget_(self)
            node.setAction_(action)
            view.addSubview_(node)

        self.settings_window = window
        self.settings_document = document
        self.refreshWidgetControls()
        document.scrollPoint_(NSMakePoint(0, document_height - window_height))
        self.settings_fields = {
            "symbols": symbols,
            "order": order,
            "mode": mode,
            "rotate": rotate,
            "clocks": clocks,
            "inset": inset,
            "zoom": zoom,
            "zoomValues": zoom_values,
            "align": align,
            "alignNames": align_names,
            "lang": lang,
            "priority": priority,
            "priorityNames": priority_names,
            "blocks": blocks,
            "chart": chart,
            "chartNames": chart_names,
        }

    rotate_values = (0, 5, 10, 20, 30, 60)

    @objc.python_method
    def widgetSummary(self):
        return f"WIDGETS · {len(load_widgets(self.config))} · 编辑栏可增删"

    def clearWidgets_(self, _sender=None):
        store_widgets(self.config, [])
        drop_widget_anchors(self.config)
        save_defaults(self.config)
        self.refreshWidgetControls()
        note = getattr(self, "widget_note", None)
        if note is not None:
            note.setStringValue_("组件已清空")
        self.buildScreens_(None)
        log("widgets cleared from panel")

    def applyPreset_(self, sender):
        index = sender.indexOfSelectedItem()
        if 0 <= index < len(PRESETS):
            self.settings_fields["symbols"].setStringValue_(PRESETS[index][1])

    def rotateChanged_(self, sender):
        """A popup pick is a real change, so apply it without a second click."""
        self.applySettings_(sender)

    def applySettings_(self, _sender=None):
        fields = self.settings_fields
        if not fields:
            return
        self.config.symbols = fields["symbols"].stringValue().strip()
        self.config.order = fields["order"].stringValue().strip()
        mode_names = [name for name, _ in MODES]
        index = fields["mode"].indexOfSelectedItem()
        if 0 <= index < len(mode_names):
            self.config.mode = mode_names[index]
        rotate_index = fields["rotate"].indexOfSelectedItem()
        if 0 <= rotate_index < len(self.rotate_values):
            self.config.rotate = int(self.rotate_values[rotate_index])
        self.config.clocks = fields["clocks"].stringValue().strip()
        edges = []
        for name in ("top", "right", "bottom", "left"):
            slider = self.inset_sliders.get(name)
            value = int(slider.intValue()) if slider is not None else 0
            edges.append(str(max(0, value)))
            tag = self.inset_values.get(name)
            if tag is not None:
                tag.setStringValue_(f"{name.upper()} {max(0, value)}px")
        self.config.inset = "" if all(edge == "0" for edge in edges) else ",".join(edges)
        self.config.blocks = fields["blocks"].stringValue().strip()
        priority_index = fields["priority"].indexOfSelectedItem()
        names = fields["priorityNames"]
        if 0 <= priority_index < len(names):
            self.config.priority = names[priority_index]
        zoom_index = fields["zoom"].indexOfSelectedItem()
        values = fields["zoomValues"]
        if 0 <= zoom_index < len(values):
            self.config.zoom = float(values[zoom_index])
        align_index = fields["align"].indexOfSelectedItem()
        names = fields["alignNames"]
        if 0 <= align_index < len(names):
            self.config.align = names[align_index]
        self.config.lang = "zh" if fields["lang"].indexOfSelectedItem() == 1 else "en"
        chart_index = fields["chart"].indexOfSelectedItem()
        chart_names = fields["chartNames"]
        if 0 <= chart_index < len(chart_names):
            self.config.chart = chart_names[chart_index]
        save_defaults(self.config)
        self.refreshWidgetControls()
        self.buildScreens_(None)

    @objc.python_method
    def applyLayoutPayload(self, payload):
        if payload is None:
            return
        anchors = payload.get("anchors") or {}
        parts = []
        for name, pair in anchors.items():
            values = [str(round(float(value), 2)) for value in pair]
            parts.append(f"{name}:{','.join(values)}")
        self.config.anchors = ";".join(parts)
        blocks = payload.get("blocks") or []
        self.config.blocks = ",".join(str(block) for block in blocks) if blocks else ""
        priority = payload.get("priority")
        if priority in ("default", "session", "order"):
            self.config.priority = priority
        if payload.get("chart") in ("line", "candles", "tv"):
            self.config.chart = payload["chart"]
        widgets = payload.get("widgets")
        if widgets is not None:
            try:
                items = [
                    {
                        "id": str(item.get("id", "")),
                        "type": str(item.get("type", "")),
                        "symbol": str(item.get("symbol", "")),
                        "zone": str(item.get("zone", "")),
                    }
                    for item in widgets
                    if item.get("type")
                ]
            except (AttributeError, TypeError):
                items = []
            store_widgets(self.config, items)
            self.refreshWidgetControls()
            log("widgets: %s" % (self.config.widgets or "none"))
        self.config.edit = False
        self.config.interactive = False
        box = getattr(self, "drag_box", None)
        if box is not None:
            box.setState_(NSControlStateValueOff)
        save_defaults(self.config)
        self.buildScreens_(None)
        log("layout from page: %s" % (self.config.anchors or "reset"))

    def enterEditMode_(self, _sender=None):
        self.config.edit = True
        self.config.interactive = True
        save_defaults(self.config)
        self.buildScreens_(None)
        log("edit mode on: menu")

    @objc.python_method
    @objc.python_method
    def evalJS(self, script):
        if not self.windows:
            return None
        script = (
            "JSON.stringify((function(){try{var l=window.DeskTapeLayout?window.DeskTapeLayout():null;"
            "return l?{anchors:l.anchors,blocks:l.blocks}:null}catch(e){return {error:String(e)}}})())"
        )
        window, _delegate = self.windows[0]
        webview = window.contentView()
        try:
            value, _error = webview.evaluateJavaScript_error_(script, None)
        except Exception:  # noqa: BLE001
            return None
        try:
            return json.loads(value or "null")
        except Exception:  # noqa: BLE001
            return None

    @objc.python_method
    def sendMouse(self, kind, point):
        window, _delegate = self.windows[0]
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
            kind,
            point,
            0,
            0.0,
            window.windowNumber(),
            None,
            0,
            1,
            1.0 if kind != 2 else 0.0,
        )
        window.sendEvent_(event)

    @objc.python_method
    def postMouse(self, kind, point):
        window, _delegate = self.windows[0]
        event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
            kind,
            point,
            0,
            0.0,
            window.windowNumber(),
            None,
            0,
            1,
            1.0 if kind != 2 else 0.0,
        )
        NSApplication.sharedApplication().postEvent_atStart_(event, False)

    @objc.python_method
    def startDragTest(self):
        import traceback

        try:
            self._dragTestBody()
        except Exception:  # noqa: BLE001
            log("dragtest failed: %s" % traceback.format_exc().replace("\n", " | "))

    @objc.python_method
    def runCgTest(self):
        """Post an OS-level click at the hero block and report whether the page saw it."""
        import traceback

        try:
            report = {}
            window, _delegate = self.windows[0]
            frame = window.frame()
            width, height = frame.size.width, frame.size.height
            screen = NSScreen.mainScreen().frame()
            # Window coords are bottom-left; CGEvent uses top-left global coords.
            point_x = frame.origin.x + width * 0.28
            point_y = screen.size.height - (frame.origin.y + height * 0.50)
            report["window_level"] = window.level()
            report["ignores_mouse"] = bool(window.ignoresMouseEvents())
            report["click_point"] = [round(point_x, 1), round(point_y, 1)]
            report["events_before"] = len(self.probe_events)

            attempts = {}
            for label, pid in (("hid", None), ("pid", os.getpid())):
                before = len(self.probe_events)
                post_mouse(5, point_x, point_y, pid)
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.15))
                post_mouse(1, point_x, point_y, pid)
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.15))
                post_mouse(2, point_x, point_y, pid)
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.6))
                attempts[label] = len(self.probe_events) - before
            report["attempts"] = attempts
            report["probe_events"] = list(self.probe_events)
            report["delivered"] = len(self.probe_events) > report["events_before"]
            report["delivered_by"] = [k for k, v in attempts.items() if v]
            report["ax_trusted"] = bool(
                ctypes.CDLL(
                    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
                ).AXIsProcessTrusted()
            )
            Path("/tmp/desk_tape_cgtest.json").write_text(
                json.dumps(report, indent=2, default=str), encoding="utf-8"
            )
            log("cgtest: delivered=%s events=%s" % (report["delivered"], len(self.probe_events)))
            NSApplication.sharedApplication().terminate_(None)
        except Exception:  # noqa: BLE001
            log("cgtest failed: %s" % traceback.format_exc().replace("\n", " | "))
            NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _dragTestBody(self):
        report = {}
        window, _delegate = self.windows[0]
        report["level_editing_live"] = window.level()
        report["ignores_mouse_while_editing"] = bool(window.ignoresMouseEvents())
        frame = window.frame()
        width, height = frame.size.width, frame.size.height
        start = NSMakePoint(width * 0.28, height * 0.50)
        end = NSMakePoint(width * 0.28 + 150, height * 0.50 + 90)

        report["before"] = self.config.anchors or ""
        # Enqueue the events on NSApplication's own queue: this is the same
        # dispatch path a click from the window server takes once it reaches the
        # app (NSApplication -> window -> hit test -> WKWebView), rather than
        # calling window.sendEvent_ directly.
        self.postMouse(1, start)
        self.postMouse(6, end)
        self.postMouse(2, end)
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.5))
        self.saveLayout_(None)
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(1.0))
        report["after"] = self.config.anchors or ""
        report["dragged"] = report["before"] != report["after"]

        if self.windows:
            window, _delegate = self.windows[0]
        report["level_after_exit_live"] = window.level()
        report["ignores_mouse_after_exit"] = bool(window.ignoresMouseEvents())
        report["click_through"] = bool(
            report["ignores_mouse_after_exit"]
            and report["level_after_exit_live"] < FINDER_DESKTOP_LEVEL
        )
        Path("/tmp/desk_tape_dragtest.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        log("dragtest: dragged=%s click_through=%s" % (report["dragged"], report["click_through"]))
        NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def scheduleEditTimeout(self, seconds=150):
        """Dragging needs a click-receiving window; never leave the desktop captured."""
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            float(seconds),
            False,
            lambda _timer: self.leaveEditMode("timeout"),
        )

    @objc.python_method
    def leaveEditMode(self, reason="done"):
        if not getattr(self.config, "edit", False):
            return
        self.config.edit = False
        self.config.interactive = False
        box = getattr(self, "drag_box", None)
        if box is not None:
            box.setState_(NSControlStateValueOff)
        save_defaults(self.config)
        self.buildScreens_(None)
        log("edit mode off: %s" % reason)

    def toggleEdit_(self, sender):
        editing = sender.state() == NSControlStateValueOn
        self.config.edit = editing
        if editing:
            self.config.interactive = True
        save_defaults(self.config)
        self.buildScreens_(None)
        log("edit mode: %s" % editing)

    def saveLayout_(self, _sender=None):
        if not self.windows:
            return
        window, _delegate = self.windows[0]
        webview = window.contentView()
        # Ask the page to push its layout over the script-message bridge. Blocks with
        # completion handlers are unavailable in this PyObjC build, so the page owns
        # the round trip and the host just asks.
        webview.evaluateJavaScript_completionHandler_(
            "window.DeskTapePost && window.DeskTapePost()", None
        )

    def closeSettings_(self, _sender):
        if self.settings_window is not None:
            self.settings_window.orderOut_(None)

    def reload_(self, _sender):
        for window, delegate in self.windows:
            webview = window.contentView()
            if delegate.url is not None:
                webview.loadRequest_(NSURLRequest.requestWithURL_(delegate.url))

    def reveal_(self, _sender):
        bundle = bundle_path()
        target = str(bundle or Path(__file__).resolve().parent)
        NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(target, "")

    def openTerminal_(self, _sender):
        rotate = f"&rotate={self.config.rotate}" if getattr(self.config, "rotate", 0) else ""
        target = f"{_trim(self.config.url)}/wallpaper?mode={self.config.mode}{rotate}"
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(target))

    def toggleWallpaperVisibility_(self, sender):
        """Make the wallpaper disappear and come back; proves what draws on top of what."""
        self.wallpaper_visible = sender.state() == NSControlStateValueOn
        for window, _delegate in self.windows:
            if self.wallpaper_visible:
                window.orderFrontRegardless()
            else:
                window.orderOut_(None)
        log("wallpaper visible=%s" % self.wallpaper_visible)

    def toggleLogin_(self, sender):
        if login_installed():
            login_uninstall()
        else:
            login_install(self.config)
        sender.setState_(NSControlStateValueOn if login_installed() else NSControlStateValueOff)

    def quit_(self, _sender):
        NSApplication.sharedApplication().terminate_(None)

    # --------------------------------------------------------------- lifecycle
    @objc.python_method
    def installOpenHandlers(self):
        """Catch double-click / `open` on a running LSUIElement app."""
        manager = NSAppleEventManager.sharedAppleEventManager()
        # Four-char codes as integers: 'aevt'/'oapp' and 'aevt'/'rapp'
        for event_id in (0x6F617070, 0x72617070):
            try:
                manager.setEventHandler_andSelector_forEventClass_andEventID_(
                    self, "handleOpenApplicationEvent:withReplyEvent:", 0x61657674, event_id
                )
            except Exception as error:  # noqa: BLE001
                log("event handler install failed id=%r: %r" % (event_id, error))

    def handleOpenApplicationEvent_withReplyEvent_(self, event, reply):
        log("open application event: showing settings")
        self.openSettings_(None)

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        log("reopen event: showing settings")
        self.openSettings_(None)
        return False

    def applicationDidFinishLaunching_(self, _notification):
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.buildScreens_(None)
        self.buildMenu()
        self.installOpenHandlers()
        log("launched: mode=%s rotate=%s" % (self.config.mode, getattr(self.config, "rotate", 0)))
        log("page: %s" % (page_url(self.config) or "—"))
        if getattr(self.config, "edit", False):
            self.scheduleEditTimeout()
        if getattr(self.config, "settings", False):
            self.openSettings_(None)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "screensChanged:", NSApplicationDidChangeScreenParametersNotification, None
        )
        self.probe_events = []
        if getattr(self.config, "cgtest", False) or getattr(self.config, "dragtest", False):
            self.config.probe = True
        if getattr(self.config, "cgtest", False):
            objc.lookUpClass("NSTimer").scheduledTimerWithTimeInterval_repeats_block_(
                5.0, False, lambda _timer=None: self.runCgTest()
            )
        if getattr(self.config, "dragtest", False):
            timer = objc.lookUpClass("NSTimer").scheduledTimerWithTimeInterval_repeats_block_(
                5.0, False, lambda _timer=None: self.startDragTest()
            )
            log("dragtest: scheduled timer=%s" % (timer is not None,))
        if self.config.selftest:
            from AppKit import NSTimer

            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                6.0, False, lambda _timer: self.runSelftest_()
            )

    # ---------------------------------------------------------------- selftest
    def runSelftest_(self, _timer=None):
        """Write window/page diagnostics (and a snapshot) so a launch can be checked
        without a human looking at the screen. Used by scripts/verify-wallpaper.sh."""
        import json

        window, delegate = self.windows[0]
        webview = window.contentView()
        frame = window.frame()
        out = os.environ.get("DESK_TAPE_SELFTEST_OUT", "/tmp/desk_tape_selftest.json")
        result = {
            "screens": len(self.windows),
            "level": int(window.level()),
            "frame": [int(frame.size.width), int(frame.size.height)],
            "ignoresMouseEvents": bool(window.ignoresMouseEvents()),
            "url": delegate.url.absoluteString() if delegate.url else None,
        }

        def finish(payload):
            payload.update(result)
            try:
                Path(out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            finally:
                NSApplication.sharedApplication().terminate_(None)

        def snapshot():
            bounds = webview.bounds()
            rep = webview.bitmapImageRepForCachingDisplayInRect_(bounds)
            if rep is None:
                return None
            webview.cacheDisplayInRect_toBitmapImageRep_(bounds, rep)
            data = rep.representationUsingType_properties_(4, {})  # NSBitmapImageFileTypePNG
            target = str(Path(out).with_suffix(".png"))
            if data is not None and data.writeToFile_atomically_(target, True):
                return target
            return None

        def with_count(title, error):
            def done(count, count_error):
                finish(
                    {
                        "title": title,
                        "titleError": str(error) if error else None,
                        "renderedNodes": count,
                        "countError": str(count_error) if count_error else None,
                        "snapshot": snapshot(),
                    }
                )

            webview.evaluateJavaScript_completionHandler_(
                "document.querySelectorAll('.tile, #hero-price, .mh-price').length", done
            )

        webview.evaluateJavaScript_completionHandler_("document.title", with_count)


def login_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_LABEL}.plist"


def login_installed() -> bool:
    return login_plist_path().exists()


def login_install(config) -> None:
    script = Path(__file__).resolve()
    payload = {
        "Label": LOGIN_LABEL,
        "ProgramArguments": [
            sys.executable,
            str(script),
            "--url",
            config.url,
            "--mode",
            config.mode,
            "--rotate",
            str(getattr(config, "rotate", 0)),
            "--rotate-modes",
            getattr(config, "rotate_modes", ""),
            "--order",
            getattr(config, "order", ""),
            "--clocks",
            getattr(config, "clocks", ""),
            "--inset",
            getattr(config, "inset", ""),
            "--zoom",
            str(getattr(config, "zoom", 1.0)),
            "--align",
            getattr(config, "align", "left"),
            "--lang",
            getattr(config, "lang", "en"),
        ],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    path = login_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(payload))
    path.chmod(0o600)
    subprocess.run(["/bin/launchctl", "unload", str(path)], check=False, capture_output=True)
    subprocess.run(["/bin/launchctl", "load", "-w", str(path)], check=False, capture_output=True)


def login_uninstall() -> None:
    path = login_plist_path()
    subprocess.run(["/bin/launchctl", "unload", str(path)], check=False, capture_output=True)
    path.unlink(missing_ok=True)


def parse_args(argv=None) -> "Config":
    parser = argparse.ArgumentParser(description="Desk Tape — live market wallpaper for macOS")
    parser.add_argument("--url", default=os.environ.get("DESKTAPE_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--mode", default=os.environ.get("OMT_WALLPAPER_MODE", "horizon"), choices=[m for m, _ in MODES])
    parser.add_argument("--hero", default=os.environ.get("OMT_WALLPAPER_HERO", ""))
    parser.add_argument("--symbols", default="")
    parser.add_argument("--lang", default="en", choices=["en", "zh"])
    parser.add_argument("--refresh", type=int, default=30)
    parser.add_argument("--rotate", type=int, default=0, help="auto-switch the hero symbol every N seconds (0 = off)")
    parser.add_argument(
        "--rotate-modes",
        default="",
        help="per-layout dwell, e.g. board:0,horizon:12,focus:20 (overrides --rotate)",
    )
    parser.add_argument("--order", default="", help="priority order for auto-switch, e.g. ^GSPC,^VIX,BTC-USD")
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="let the wallpaper receive clicks (default: clicks pass through to the desktop)",
    )
    parser.add_argument("--clocks", default="", help="comma separated IANA zones for the clock strip")
    parser.add_argument("--blocks", default="", help="visible blocks: ribbon,hero,stats,chart,rail,tape,clocks,sessions,brand")
    parser.add_argument("--anchors", default="", help="dragged block positions, e.g. hero:6,38;rail:72,4")
    parser.add_argument(
        "--chart", default="tv", choices=["line", "candles", "tv"], help="price chart: TradingView, native candles, or line"
    )
    parser.add_argument("--widgets", default="", help='extra widgets as JSON, e.g. [{"id":"quote1","type":"quote","symbol":"^GSPC"}]')
    parser.add_argument(
        "--priority",
        default="default",
        choices=["default", "session", "order"],
        help="ordering of lists: payload order, open exchanges first, or --order only",
    )
    parser.add_argument("--inset", default="", help="layout insets, e.g. top:60,right:320 or 60,320,40,0")
    parser.add_argument("--zoom", type=float, default=1.0, help="content scale, 0.7 – 1.35")
    parser.add_argument("--align", default="left", choices=["left", "center", "right"], help="hero alignment")
    parser.add_argument("--edit", action="store_true", help="start with draggable blocks (implies --interactive)")
    parser.add_argument("--dragtest", action="store_true", help="internal: synthesise a real mouse drag and report")
    parser.add_argument("--cgtest", action="store_true", help="internal: post OS-level clicks and report delivery")
    parser.add_argument("--opacity", type=float, default=1.0)
    parser.add_argument("--settings", action="store_true", help="open the settings panel on launch")
    parser.add_argument("--selftest", action="store_true", help="load the page, print diagnostics, then quit")
    parser.add_argument("--install-agent", action="store_true", help="start Desk Tape at login, then exit")
    parser.add_argument("--uninstall-agent", action="store_true", help="remove the login item, then exit")
    return parser.parse_args(argv)


def main() -> int:
    config = parse_args()
    if config.install_agent:
        login_install(config)
        print(f"Installed login item at {login_plist_path()}")
        return 0
    if config.uninstall_agent:
        login_uninstall()
        print("Removed the Desk Tape login item")
        return 0

    config = apply_defaults(config)
    application = NSApplication.sharedApplication()
    controller = Controller.alloc().initWithConfig_(config)
    application.setDelegate_(controller)
    application.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
