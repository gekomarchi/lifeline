"""Records the public board while the capture step runs: one full-page picture every few seconds.

    python demo/board_live.py <name> [--seconds <n>] [--every <seconds>] [--url <board URL>]

The board reads the three chains in the browser, with "Auto read" at 30 s. Each picture has the time of
the machine in its file name, so a picture can be set against a line of the terminal recording.
Output: demo/footage/<name>/<epoch milliseconds>.png, 3840 pixels wide. Stop it with Ctrl-C or SIGTERM.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BOARD = "https://gekomarchi.github.io/lifeline/board/"
CHROME = "/usr/bin/google-chrome"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name")
    parser.add_argument("--seconds", type=float, default=900.0)
    parser.add_argument("--every", type=float, default=3.0)
    parser.add_argument("--url", default=BOARD)
    args = parser.parse_args()
    out = Path(__file__).resolve().parent / "footage" / args.name
    out.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME, headless=True, args=["--hide-scrollbars"])
        page = browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=3,
                                   color_scheme="dark").new_page()
        page.goto(args.url + "#theme=dark", wait_until="domcontentloaded")
        page.get_by_label("Auto read").select_option("30")
        end = time.monotonic() + args.seconds
        try:
            while time.monotonic() < end:
                started = time.monotonic()
                page.screenshot(path=str(out / f"{int(time.time() * 1000)}.png"), full_page=True)
                time.sleep(max(args.every - (time.monotonic() - started), 0.1))
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
