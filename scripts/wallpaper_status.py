#!/usr/bin/env python3
"""Check that Desk Tape really is running as a wallpaper, under the Finder desktop.

Prints one line per check plus a verdict, so "is it installed?" is answerable
without guessing. Exit code 0 = everything correct, 1 = something to fix.

Usage:
    python3.12 scripts/wallpaper_status.py
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path

EXPECTED_LEVEL = -2147483610  # between system wallpaper and the Finder desktop
FINDER_LEVEL = -2147483603
SYSTEM_WALLPAPER_LEVEL = -2147483625
WALLPAPER_SOURCE = Path.home() / "Applications" / "DeskTape.app"
SETTINGS = Path.home() / "Library" / "Application Support" / "DeskTape" / "settings.json"
DESKTOP_LAUNCHER = Path.home() / "Desktop" / "Desk Tape.app"
LAUNCHER_ID = "com.openmarket.desktape.launcher"


def window_rows() -> list[tuple[int, str, tuple[int, int, int, int] | None]]:
    cg = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CFArrayGetCount.restype = ctypes.c_long
    cg.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cg.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cg.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    cg.CFDictionaryGetValue.restype = ctypes.c_void_p
    cg.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cg.CFStringCreateWithCString.restype = ctypes.c_void_p
    cg.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cg.CFNumberGetValue.restype = ctypes.c_bool
    cg.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    cg.CFStringGetCString.restype = ctypes.c_bool
    cg.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

    def key(name: str) -> ctypes.c_void_p:
        return cg.CFStringCreateWithCString(None, name.encode(), 0x08000100)

    def number(entry: ctypes.c_void_p, field: ctypes.c_void_p) -> int:
        value = cg.CFDictionaryGetValue(entry, field)
        out = ctypes.c_double(0)
        if value:
            cg.CFNumberGetValue(value, 13, ctypes.byref(out))
        return int(out.value)

    owner_key, layer_key, bounds_key = (
        key("kCGWindowOwnerName"),
        key("kCGWindowLayer"),
        key("kCGWindowBounds"),
    )
    listing = cg.CGWindowListCopyWindowInfo(0, 0)
    buffer = ctypes.create_string_buffer(256)
    rows: list[tuple[int, str, tuple[int, int, int, int] | None]] = []
    for index in range(cg.CFArrayGetCount(listing)):
        entry = cg.CFArrayGetValueAtIndex(listing, index)
        owner = cg.CFDictionaryGetValue(entry, owner_key)
        if not owner:
            continue
        cg.CFStringGetCString(owner, buffer, 256, 0x08000100)
        bounds = cg.CFDictionaryGetValue(entry, bounds_key)
        box = None
        if bounds:
            box = tuple(number(bounds, key(axis)) for axis in ("X", "Y", "Width", "Height"))
        rows.append((number(entry, layer_key), buffer.value.decode(), box))
    return rows


def processes() -> list[str]:
    result = subprocess.run(
        ["pgrep", "-fl", "DeskTape.app"], capture_output=True, text=True, check=False
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    checks: list[tuple[bool, str]] = []

    procs = processes()
    checks.append((len(procs) == 1, f"running instances: {len(procs)}"))
    for line in procs:
        print(f"  · {line}")

    rows = window_rows()
    tape = [(layer, box) for layer, owner, box in rows if owner == "DeskTape"]
    full = [(layer, box) for layer, box in tape if box and box[2] > 1000 and box[3] > 700]
    checks.append((bool(full), f"wallpaper window present: {bool(full)}"))
    for layer, box in full:
        print(f"  · wallpaper layer={layer} bounds={box}")
        checks.append(
            (
                layer == EXPECTED_LEVEL,
                f"wallpaper layer is {EXPECTED_LEVEL}: got {layer}",
            )
        )
        checks.append(
            (
                SYSTEM_WALLPAPER_LEVEL < layer < FINDER_LEVEL,
                f"stacking ok: wallpaper {SYSTEM_WALLPAPER_LEVEL} < tape {layer} < Finder {FINDER_LEVEL}",
            )
        )

    panel = [(layer, box) for layer, box in tape if layer == 3]
    print(
        "  · settings panel: %s"
        % ("open" if panel else "closed (click the menu bar icon to open it)")
    )

    launcher_plist = DESKTOP_LAUNCHER / "Contents" / "Info.plist"
    launcher_exec = DESKTOP_LAUNCHER / "Contents" / "MacOS" / "Desk Tape"
    checks.append((DESKTOP_LAUNCHER.exists(), f"desktop entry: {DESKTOP_LAUNCHER}"))
    if launcher_plist.exists():
        ok_id = LAUNCHER_ID in launcher_plist.read_text(encoding="utf-8", errors="ignore")
        checks.append((ok_id, f"desktop entry bundle id: {LAUNCHER_ID}"))
    else:
        checks.append((False, f"desktop entry Info.plist missing: {launcher_plist}"))
    if launcher_exec.exists():
        points_at = str(WALLPAPER_SOURCE) in launcher_exec.read_text(encoding="utf-8", errors="ignore")
        checks.append((points_at, f"desktop entry launches {WALLPAPER_SOURCE}"))
        print(f"  · desktop entry: {DESKTOP_LAUNCHER}")
    else:
        checks.append((False, f"desktop entry executable missing: {launcher_exec}"))

    if SETTINGS.exists():
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except ValueError:
            saved = {}
        checks.append((bool(saved), f"settings file ok: {SETTINGS}"))
        print(f"  · mode={saved.get('mode')} rotate={saved.get('rotate')} order={saved.get('order')!r}")
    else:
        checks.append((False, f"settings file missing: {SETTINGS}"))

    print()
    failed = [message for ok, message in checks if not ok]
    for ok, message in checks:
        print(("PASS  " if ok else "FAIL  ") + message)
    if failed:
        print("\nverdict: needs attention — run ./scripts/wallpaper-reset.sh")
        return 1
    print("\nverdict: wallpaper is live, below Finder and above the system wallpaper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
