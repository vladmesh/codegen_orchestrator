"""Polish task 1 check: branding, demo sidebar, story page auto-refresh.
Run: uv run --with playwright python demo/shots_polish1.py [base_url]"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent / "shots" / "polish-1"
OUT.mkdir(parents=True, exist_ok=True)
STORY = "story-7c41e9a2"


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST" if cmd != "state" else "GET")
    return json.load(urllib.request.urlopen(req))


report = {}
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)

    ctl("reset")
    page.goto(BASE + "/")
    page.wait_for_load_state("networkidle")
    report["title"] = page.title()
    report["favicon_status"] = page.request.get(BASE + "/favicon.ico").status
    report["sidebar_items"] = page.locator("aside nav a").all_inner_texts()
    report["brand"] = page.locator("aside > div").first.inner_text()
    page.locator("aside").screenshot(path=str(OUT / "01-sidebar.png"))
    page.screenshot(path=str(OUT / "02-tab-attention.png"))

    # Tab strip is browser chrome (not in headless screenshots): render title + favicon as a fake tab.
    tab = browser.new_page(viewport={"width": 420, "height": 60})
    tab.set_content(
        f"<body style='margin:0;background:#dee1e6;font:13px system-ui'>"
        f"<div style='margin:10px;display:inline-flex;gap:8px;align-items:center;background:#fff;"
        f"padding:8px 14px;border-radius:8px 8px 0 0;width:220px'>"
        f"<img src='{BASE}/favicon-32.png' width=16 height=16><span>{report['title']}</span></div></body>"
    )
    tab.wait_for_timeout(300)
    tab.screenshot(path=str(OUT / "03-tab-mock.png"))
    tab.close()

    # Story page: start accelerated scene, keep the page open (no reload) and watch statuses change.
    ctl("start?speed=4&at=30")
    page.goto(f"{BASE}/stories/{STORY}")
    page.wait_for_load_state("networkidle")
    time.sleep(1)
    snaps = []
    for i in range(4):
        text = page.locator("main").inner_text()
        snaps.append(text)
        page.screenshot(path=str(OUT / f"1{i}-story-t{i}.png"), full_page=True)
        st = ctl("state")
        print("shot", i, "scene", st)
        if i < 3:
            time.sleep(25)
    report["story_changed_without_reload"] = len(set(snaps)) > 1
    report["nav_count"] = page.evaluate("performance.getEntriesByType('navigation').length")
    browser.close()
ctl("reset")
print(json.dumps(report, indent=2))
