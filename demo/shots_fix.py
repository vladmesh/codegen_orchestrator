"""Owner-feedback fix check: screenshots into demo/shots/fix-*.png.
Usage: /tmp/pdfenv/bin/python demo/shots_fix.py <name> <scene_second|idle> <path> <width> [js-before-shot] [full]
Example: shots_fix.py kanban-390 150 /journeys/story-7c41e9a2 390
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent / "shots"


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST" if cmd != "state" else "GET")
    return json.load(urllib.request.urlopen(req))


def shoot(name, at, path, width, js=None, full=False, height=None, selector=None):
    if at == "idle":
        ctl("reset")
    else:
        ctl("reset")
        ctl(f"start?speed=0.0001&at={at}")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": int(width), "height": height or (900 if int(width) > 600 else 844)},
                        device_scale_factor=1 if int(width) > 600 else 2)
        pg.goto(BASE + path)
        pg.wait_for_timeout(2500)
        if js:
            pg.evaluate(js)
            pg.wait_for_timeout(1200)
        if selector:
            pg.locator(selector).first.scroll_into_view_if_needed()
            pg.wait_for_timeout(500)
        out = OUT / f"fix-{name}.png"
        pg.screenshot(path=str(out), full_page=full)
        print(out)
        b.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    shoot(a[0], a[1], a[2], a[3], a[4] if len(a) > 4 and a[4] else None, len(a) > 5 and a[5] == "full",
          selector=a[6] if len(a) > 6 else None)
