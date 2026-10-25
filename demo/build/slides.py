from pathlib import Path
from playwright.sync_api import sync_playwright
d = Path(__file__).resolve().parent.parent / "slides"
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    for f in sorted(d.glob("s*.html")):
        pg.goto(f.as_uri()); pg.wait_for_timeout(300)
        pg.screenshot(path=str(d.parent / "build" / (f.stem + ".png")))
        print(f.stem)
    b.close()
