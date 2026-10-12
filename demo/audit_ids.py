"""Audit visible text on scene-1 screens for raw ids, snake_case labels and US-locale dates.
Run: uv run --with playwright python demo/audit_ids.py [base_url] [out.json]"""
import json
import re
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/audit_ids.json"
STORY = "story-7c41e9a2"
PROJECT = "6b1f0c2e-9a4d-4e57-8c3b-2f1e7a90d1c5"
TASK = "a1c3e5f7-3333-4a2b-9c01-000000000003"

PATTERNS = {
    "uuid": re.compile(r"[0-9a-f]{8}-"),
    "story_id": re.compile(r"story-[0-9a-f]+"),
    "hex": re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{10,}\b"),
    "wkr": re.compile(r"wkr-[0-9a-z-]+|run-[a-z0-9-]+|ci-run-\d|req-[0-9a-f]+"),
    "snake": re.compile(r"\b[a-z]+_[a-z_]+\b"),
    "usdate": re.compile(r"\d{1,2}/\d{1,2}/\d{4}"),
}


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST" if cmd != "state" else "GET")
    return json.load(urllib.request.urlopen(req))


def pages_at(at):
    p = ["/", "/journeys", "/atlas", "/workers", "/overview", "/projects", "/tasks", "/users"]
    if at is not None:
        p += [f"/journeys/{STORY}", f"/stories/{STORY}", f"/projects/{PROJECT}", f"/tasks/{TASK}", "/users/1042"]
        if at >= 495:
            p.append("/applications/107")
        if 60 < at < 230:
            p.append("/workers/wkr-3f9c2a71d0e4")
    return p


report = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    for at in (None, 40, 150, 400, 520, 600):
        ctl("reset") if at is None else ctl(f"start?speed=0&at={at}")
        for path in pages_at(at):
            page.goto(BASE + path)
            page.wait_for_load_state("networkidle")
            time.sleep(0.4)
            text = page.locator("main").inner_text()
            hits = {k: sorted(set(rx.findall(text))) for k, rx in PATTERNS.items()}
            hits = {k: v for k, v in hits.items() if v}
            report.append({"at": at, "path": path, "hits": hits})
            print(at, path, json.dumps(hits))
    browser.close()
ctl("reset")
json.dump(report, open(OUT, "w"), indent=1)
