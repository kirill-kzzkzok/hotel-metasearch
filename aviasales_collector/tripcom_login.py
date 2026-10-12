from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description="Open a persistent Trip.com Chromium profile for manual sign-in.")
    default_profile = Path(__file__).resolve().parent.parent / ".tripcom-browser"
    parser.add_argument("--profile", default=os.getenv("TRIP_COM_BROWSER_DATA_DIR", str(default_profile)))
    args = parser.parse_args()
    profile = Path(args.profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), headless=False, locale="ru-RU", viewport={"width": 1440, "height": 1000}
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://ru.trip.com/account/signin", wait_until="domcontentloaded", timeout=45000)
        print("Войдите вручную в открытом Chromium. Не решайте challenge автоматически.")
        print("После входа закройте окно Chromium; профиль останется сохранённым.")
        while not page.is_closed():
            time.sleep(1)
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
