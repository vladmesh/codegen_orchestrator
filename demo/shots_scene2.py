"""Scene 2 key screens at 1920×1080, frozen at chosen scene seconds → demo/shots/scene2/*.png.
Run: /tmp/pdfenv/bin/python demo/shots_scene2.py   (leaves the dashboard in reset of scene 2)"""
import json
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent / "shots" / "scene2"
OUT.mkdir(parents=True, exist_ok=True)
STORY = "story-3e8b5d10"
PROJECT = "3a9d7e41-6c20-4b8f-9d15-7e0c2b4f6a18"


def ctl(cmd):
    return json.loads(urllib.request.urlopen(urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST")).read())


def freeze(at):
    ctl(f"start?speed=0.0001&at={at}")


SHOTS = [
    # (name, second, url, action)
    ("01-journeys", 4, "/journeys", None),
    ("02-journey-planning", 16, f"/journeys/{STORY}", "reqs"),
    ("03-journey-catalog", 26, f"/journeys/{STORY}", None),
    ("04-journey-catalog-step", 36, f"/journeys/{STORY}", "catalog"),
    ("05-journey-coding", 75, f"/journeys/{STORY}", None),
    ("06-journey-repo", 80, f"/journeys/{STORY}", "repo"),
    ("07-journey-ci", 150, f"/journeys/{STORY}", None),
    ("08-journey-deploy", 185, f"/journeys/{STORY}", None),
    ("09-journey-qa-module-pr", 216, f"/journeys/{STORY}", "bottom"),
    ("10-journey-done", 232, f"/journeys/{STORY}", None),
    ("11-journey-done-catalog", 232, f"/journeys/{STORY}", "catalog"),
    ("12-journey-passport", 232, f"/journeys/{STORY}", "bottom"),
    ("13-atlas-layers", 232, "/atlas", None),
    ("14-atlas-tg-reader", 232, "/atlas?view=layers&kind=platform&id=tg-reader", None),
    ("15-atlas-product", 232, f"/atlas?view=product&product={PROJECT}&kind=product&id={PROJECT}", None),
    ("16-atlas-matrix", 232, "/atlas?view=matrix", None),
    ("17-workers", 70, "/workers", None),
    ("18-project", 232, f"/projects/{PROJECT}", None),
    ("19-atlas-before", 10, "/atlas?view=layers&kind=platform&id=tg-reader", None),
]

ctl("reset?scene=2")
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1920, "height": 1080})
    pg.goto(BASE + "/journeys")
    pg.evaluate("localStorage.clear()")
    for name, at, url, action in SHOTS:
        freeze(at)
        pg.goto(BASE + url)
        pg.wait_for_timeout(2200)
        if action == "reqs":
            pg.get_by_role("button", name="requirements").first.click()
            pg.wait_for_timeout(600)
        if action == "catalog":
            pg.get_by_role("tab").filter(has_text="Catalog").click()
            pg.wait_for_timeout(600)
        if action == "repo":
            pg.locator("#repo-panel").scroll_into_view_if_needed()
            pg.wait_for_timeout(600)
        if action == "bottom":
            pg.evaluate("document.querySelector('main').scrollTo({top: 99999})")
            pg.wait_for_timeout(600)
        pg.screenshot(path=str(OUT / f"{name}.png"))
        print(name, at)
    b.close()
ctl("reset?scene=2")
print(ctl("reset"))
