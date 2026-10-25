"""Render dashboard screenshots headlessly (uses the system Chrome).  python scripts/screenshots.py http://localhost:8765"""

import sys

from playwright.sync_api import sync_playwright

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
shots = [(a.split("=", 1)[0], a.split("=", 1)[1]) for a in sys.argv[2:]]
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
    for name, run in shots:
        pg.goto(f"{base}/?mask=1&run={run}")
        pg.wait_for_selector(".ag-row", timeout=20000)
        pg.wait_for_timeout(1200)
        pg.screenshot(path=f"docs/screenshots/{name}.png", full_page=True)
        print("saved", name)
    b.close()
