"""Screenshot key moments of scene 1. Run: uv run --with playwright python demo/shots.py [base_url]"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent.parent / "shots"
OUT.mkdir(exist_ok=True)
STORY = "story-7c41e9a2"


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST")
    return json.load(urllib.request.urlopen(req))


# (scene second, page path, file name). speed=0 freezes the clock so each shot is exact.
SHOTS = [
    (None, "/", "00-idle-attention.png"),
    (None, "/journeys", "01-idle-journeys.png"),
    (12, "/journeys", "02-new-story-appears.png"),
    (40, f"/journeys/{STORY}", "03-architect-plan.png"),
    (40, f"/stories/{STORY}", "04-architect-tasks.png"),
    (150, "/workers", "05-workers-busy.png"),
    (150, f"/journeys/{STORY}", "06-building.png"),
    (400, "/", "07-ci-failed-attention.png"),
    (400, f"/journeys/{STORY}", "08-ci-retry.png"),
    (520, f"/journeys/{STORY}", "09-deploying.png"),
    (600, f"/journeys/{STORY}", "10-live.png"),
    (600, "/atlas", "11-atlas-live.png"),
]

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
    for at, path, name in SHOTS:
        if at is None:
            ctl("reset")
        else:
            ctl(f"start?speed=0&at={at}")
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        time.sleep(0.6)
        page.screenshot(path=str(OUT / name), full_page=True)
        print("shot", name)
    browser.close()
ctl("reset")
