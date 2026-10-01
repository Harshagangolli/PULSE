#!/usr/bin/env python3
"""pulse — terminal system monitor. Python 3.8+, stdlib only, Linux."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pulseui import __version__  # noqa: E402
from pulseui.app import App, run_json, run_once  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pulse", description=__doc__)
    ap.add_argument("-i", "--interval", type=int, default=2000, metavar="MS",
                    help="refresh interval in milliseconds (default 2000)")
    ap.add_argument("--no-mouse", action="store_true", help="disable mouse reporting")
    ap.add_argument("--once", action="store_true",
                    help="render a single frame to stdout and exit")
    ap.add_argument("--json", action="store_true",
                    help="print one JSON snapshot and exit")
    ap.add_argument("-v", "--version", action="version", version="pulse " + __version__)
    a = ap.parse_args(argv)

    if sys.platform != "linux":
        sys.stderr.write("pulse reads /proc and /sys; it only runs on Linux.\n")
        return 2

    interval = max(100, min(10000, a.interval))
    if a.json:
        return run_json()
    if a.once or not sys.stdout.isatty():
        return run_once(interval)

    app = App(interval_ms=interval, mouse=not a.no_mouse)
    try:
        return app.run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
