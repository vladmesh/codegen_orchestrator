"""Feedback 8 check: drag the panel separators like in an IDE, shots before/after into demo/shots/fix-8-*.png.
Run: /tmp/pdfenv/bin/python demo/shots_fix8.py"""
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent / "shots"
STORY = "story-7c41e9a2"


def ctl(cmd):
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST")).read()


def drag(page, sep_index, dx):
    sep = page.locator("[role=separator]").nth(sep_index)
    sep.scroll_into_view_if_needed()
    box = sep.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    for i in range(1, 11):
        page.mouse.move(x + dx * i / 10, y)
        time.sleep(0.03)
    page.mouse.up()
    time.sleep(0.8)


def widths(page):
    return page.evaluate("""[...document.querySelectorAll('[data-panel]')].map(p => Math.round(p.getBoundingClientRect().width))""")


ctl("reset")
ctl("start?speed=0.0001&at=300")
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.goto(f"{BASE}/journeys/{STORY}")
    pg.evaluate("localStorage.clear()")
    pg.reload()
    pg.wait_for_timeout(2500)
    print("separators:", pg.locator("[role=separator]").count(), "panels:", widths(pg))
    # 1) board | live log: widen the board so it goes to one row
    pg.locator("#task-board").scroll_into_view_if_needed()
    pg.wait_for_timeout(400)
    pg.screenshot(path=str(OUT / "fix-8-board-before-1440.png"))
    drag(pg, 1, 330)
    pg.locator("#task-board").scroll_into_view_if_needed()
    pg.screenshot(path=str(OUT / "fix-8-board-after-1440.png"))
    print("after board drag:", widths(pg))
    # 2) timeline | step detail: widen the details panel
    pg.evaluate("document.querySelector('main').scrollTo({top: 0})")
    pg.wait_for_timeout(300)
    pg.screenshot(path=str(OUT / "fix-8-timeline-before-1440.png"))
    drag(pg, 0, -300)
    pg.evaluate("document.querySelector('main').scrollTo({top: 0})")
    pg.screenshot(path=str(OUT / "fix-8-timeline-after-1440.png"))
    print("after timeline drag:", widths(pg))
    # 3) persisted across reload
    pg.reload()
    pg.wait_for_timeout(2500)
    print("after reload:", widths(pg))
    # 4) repo tree | diff
    drag(pg, 2, 200)
    pg.locator("[aria-label=Files]").scroll_into_view_if_needed()
    pg.screenshot(path=str(OUT / "fix-8-repo-after-1440.png"))
    pg.evaluate("localStorage.clear()")
    # 5) narrow: stacked, no handles
    m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    m.goto(f"{BASE}/journeys/{STORY}")
    m.wait_for_timeout(2500)
    print("390 separators:", m.locator("[role=separator]").count(),
          "scrollWidth:", m.evaluate("document.documentElement.scrollWidth"))
    m.locator("#task-board").scroll_into_view_if_needed()
    m.screenshot(path=str(OUT / "fix-8-stacked-390.png"))
    b.close()
ctl("reset")
