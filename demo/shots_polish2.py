"""Polish task 2 check: readable names, dates and empty states across scene 1.
Starts the scene accelerated and screenshots key screens at the beginning, middle and live,
asserting that no raw id is visible. Run: uv run --with playwright python demo/shots_polish2.py [base_url] [speed]"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
SPEED = float(sys.argv[2]) if len(sys.argv) > 2 else 8
OUT = Path(__file__).resolve().parent / "shots" / "polish-2"
OUT.mkdir(parents=True, exist_ok=True)
STORY = "story-7c41e9a2"
ID_RX = [re.compile(r"[0-9a-f]{8}-"), re.compile(r"story-[0-9a-f]+"), re.compile(r"\d{1,2}/\d{1,2}/\d{4}"),
         re.compile(r"wkr-|run-eng|ci-run-|product_owner")]

# (scene second to wait for, path, file, optional click on a station label)
SHOTS = [
    (None, "/", "00-idle-attention.png", None),
    (30, "/journeys", "01-journeys-new.png", None),
    (40, f"/journeys/{STORY}", "02-journey-plan.png", None),
    (45, f"/stories/{STORY}", "03-story-tasks.png", None),
    (60, "/", "04-attention-building.png", None),
    (150, "/workers", "05-workers.png", None),
    (160, f"/journeys/{STORY}", "06-journey-build.png", None),
    (170, "/workers/wkr-3f9c2a71d0e4", "07-worker-detail.png", None),
    (400, f"/journeys/{STORY}", "08-journey-ci-failed.png", None),
    (405, "/", "09-attention-ci.png", None),
    (410, "/tasks/a1c3e5f7-6666-4a2b-9c01-000000000006", "10-fix-task.png", None),
    (600, f"/journeys/{STORY}", "11-journey-live.png", None),
    (600, f"/journeys/{STORY}", "12-journey-live-code.png", "Code"),
    (605, "/atlas?view=product&product=6b1f0c2e-9a4d-4e57-8c3b-2f1e7a90d1c5", "13-atlas-product.png", None),
]


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST" if cmd != "state" else "GET")
    return json.load(urllib.request.urlopen(req))


leaks = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
    ctl("reset")
    started = False
    for at, path, name, click in SHOTS:
        if at is not None and not started:
            ctl(f"start?speed={SPEED}&at=0")
            started = True
        while at is not None and (ctl("state")["scene_second"] or 0) < at:
            time.sleep(0.2)
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        if click:
            page.get_by_role("tab", name=re.compile(click)).click()
        time.sleep(0.5)
        text = page.locator("main").inner_text()
        found = sorted({m for rx in ID_RX for m in rx.findall(text)})
        if found:
            leaks.append({"shot": name, "found": found})
        page.screenshot(path=str(OUT / name), full_page=True)
        print(f"{name:32} scene={ctl('state')['scene_second']} leaks={found}")
    browser.close()
ctl("reset")
print(json.dumps({"leaks": leaks}, indent=1))
sys.exit(1 if leaks else 0)
