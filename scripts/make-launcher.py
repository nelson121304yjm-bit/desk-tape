#!/usr/bin/env python3.12
"""Create a tiny launcher .app that opens the installed Desk Tape bundle.

macOS has no "shortcut" concept that keeps an icon, so the Desktop entry is a
real (but empty) app bundle: same artwork, one job — `open` the installed app.
"""

from __future__ import annotations

import plistlib
import shlex
import subprocess
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: make-launcher.py <out.app> <target.app>", file=sys.stderr)
        return 2
    bundle = Path(argv[1])
    target = Path(argv[2]).resolve()
    icon = Path("build/AppIcon.icns").resolve()

    if bundle.exists():
        subprocess.run(["rm", "-rf", str(bundle)], check=True)
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    resources = bundle / "Contents" / "Resources"
    resources.mkdir()

    if icon.exists():
        (resources / "AppIcon.icns").write_bytes(icon.read_bytes())

    executable = bundle / "Contents" / "MacOS" / "Desk Tape"
    # shlex.quote keeps a path with spaces, quotes or $() from becoming shell code.
    executable.write_text("#!/bin/sh\nexec /usr/bin/open " + shlex.quote(str(target)) + "\n")
    executable.chmod(0o755)

    payload = {
        "CFBundleName": "Desk Tape",
        "CFBundleDisplayName": "Desk Tape",
        "CFBundleIdentifier": "com.openmarket.desktape.launcher",
        "CFBundleExecutable": "Desk Tape",
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    with (bundle / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(payload, handle)

    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        check=False, capture_output=True,
    )
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
