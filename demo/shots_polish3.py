"""Polish task 3 check: the architect's task board on Journey and story pages through scene 1.
1920x1080 viewport shots; asserts no raw ids and no horizontal scroll.
Each shot starts the scene at its exact second (real speed), so frames are deterministic;
one extra shot keeps the page open across the architect's breakdown to catch cards landing.
Run: uv run --with playwright python demo/shots_polish3.py [base_url]"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent / "shots" / "polish-3"
OUT.mkdir(parents=True, exist_ok=True)
STORY = "story-7c41e9a2"
ID_RX = [re.compile(r"[0-9a-f]{8}-"), re.compile(r"story-[0-9a-f]+"), re.compile(r"wkr-|run-eng|ci-run-")]
J, S = f"/journeys/{STORY}", f"/stories/{STORY}"

# (scene second to wait for, path, file, scroll board into view)
SHOTS = [
    (12, J, "01-journey-planning.png", False),
    (45, J, "02-journey-backlog.png", False),
    (45, S, "03-story-backlog.png", True),
    (80, J, "03b-journey-build-start.png", False),
    (160, J, "04-journey-mid-build.png", False),
    (165, S, "05-story-mid-build.png", True),
    (330, J, "06-journey-pr-in-ci.png", False),
    (396, J, "07-journey-ci-failed-fix.png", False),
    (400, S, "08-story-ci-failed-fix.png", True),
    (470, J, "08b-journey-fix-in-ci.png", False),
    (600, J, "09-journey-all-done.png", False),
    (600, S, "10-story-all-done.png", True),
]


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST" if cmd != "state" else "GET")
    return json.load(urllib.request.urlopen(req))


problems = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    # Live: page open while the architect adds tasks; the next poll lands them one by one.
    ctl("reset")
    ctl("start?speed=1&at=28")
    page.goto(BASE + J)
    page.wait_for_load_state("networkidle")
    page.wait_for_function("document.querySelectorAll('[aria-labelledby=task-board] [data-card]').length > 0", timeout=20000)
    time.sleep(0.45)
    page.screenshot(path=str(OUT / "02a-journey-tasks-landing.png"))
    print("02a-journey-tasks-landing.png  scene=%.0f" % ctl("state")["scene_second"])
    for at, path, name, scroll in SHOTS:
        ctl("reset")
        ctl(f"start?speed=1&at={at}")
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        page.get_by_text("Architect’s breakdown").wait_for()
        if scroll:
            page.locator("#task-board").scroll_into_view_if_needed()
        time.sleep(0.6)
        text = page.locator("main").inner_text()
        found = sorted({m for rx in ID_RX for m in rx.findall(text)})
        hscroll = page.evaluate("Math.max(document.documentElement.scrollWidth, document.querySelector('main')?.scrollWidth ?? 0) > window.innerWidth")
        board = page.locator("#task-board").locator("xpath=..").bounding_box()
        cut = board is not None and board["y"] + board["height"] > 1080 and not scroll
        if found or hscroll:
            problems.append({"shot": name, "ids": found, "hscroll": hscroll})
        page.screenshot(path=str(OUT / name))
        print(f"{name:30} scene={ctl('state')['scene_second']:.0f} ids={found} hscroll={hscroll} board_below_fold={cut}")
    browser.close()
ctl("reset")
print(json.dumps({"problems": problems}, indent=1))
sys.exit(1 if problems else 0)
