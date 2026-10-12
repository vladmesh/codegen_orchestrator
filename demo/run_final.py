"""Task 6: scene 1 at 1x speed (600 s), walked the way the video does it, recorded at 1920x1080.
Every ~10 s: screenshot into demo/shots/final/raw-frames/ + checks (raw ids, horizontal scroll, empty/error text).
Run: uv run --with playwright python demo/run_final.py [base_url] [speed]"""
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
SPEED = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
OUT = Path(__file__).resolve().parent / "shots" / "final"
FR = OUT / "raw-frames"
shutil.rmtree(FR, ignore_errors=True)
FR.mkdir(parents=True, exist_ok=True)
ID_RX = [re.compile(r"[0-9a-f]{8}-"), re.compile(r"story-[0-9a-f]+"), re.compile(r"wkr-|run-eng|ci-run-|run-qa|run-deploy"),
         re.compile(r"undefined|NaN|null\b|\[object|\bError:|Failed to load|Something went wrong")]
problems: list[str] = []
log: list[str] = []


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST" if cmd != "state" else "GET")
    return json.load(urllib.request.urlopen(req))


def scene() -> float:
    return ctl("state")["scene_second"] or 0.0


def check(page, label):
    text = page.inner_text("body")
    for rx in ID_RX:
        hits = rx.findall(text)
        if hits:
            problems.append(f"{label} @{scene():.0f}: {rx.pattern} -> {hits[:3]}")
    sw = page.evaluate("document.documentElement.scrollWidth")
    if sw > 1920:
        problems.append(f"{label} @{scene():.0f}: horizontal scroll {sw}px")


def snap(page, label):
    s = scene()
    check(page, label)
    page.screenshot(path=str(FR / f"{int(s):03d}-{label}.png"))
    log.append(f"{s:5.0f} {label} {page.url.replace(BASE, '')}")


def hold(page, until, label, every=10.0):
    """Stay on the current screen until scene second `until`, snapping every `every` s."""
    nxt = scene()
    while True:
        s = scene()
        if s >= until:
            return
        if s >= nxt:
            snap(page, label)
            nxt = s + every
        time.sleep(0.25)


def nav(page, text):
    page.locator("aside a, nav a").filter(has_text=re.compile(rf"^\s*{text}\s*$")).first.click()
    page.wait_for_load_state("networkidle")
    time.sleep(0.4)


def scroll_to(page, sel, block="start"):
    page.evaluate(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (e) (e.closest('section') || e)"
                  f".scrollIntoView({{block: '{block}', behavior: 'smooth'}}) }})()")


def top(page):
    page.evaluate("document.querySelector('main').scrollTo({top: 0, behavior: 'smooth'})")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                              record_video_dir=str(OUT / "raw"), record_video_size={"width": 1920, "height": 1080})
    page = ctx.new_page()
    ctl("reset")
    page.goto(BASE + "/journeys")
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    snap(page, "journeys-idle")
    t0 = time.time()
    ctl(f"start?speed={SPEED}&at=0")          # = Confirm in the bot
    hold(page, 8, "journeys")
    # click into the new journey
    page.get_by_text(re.compile("Water reminder", re.I)).first.click()
    page.wait_for_load_state("networkidle")
    hold(page, 140, "journey")                  # planning → board fills → devs coding, live log
    scroll_to(page, "#repo-panel", "end")
    hold(page, 175, "journey-repo")
    top(page)
    # story page via link on journey (fall back to URL)
    story = page.locator("a[href^='/stories/']").first
    if story.count():
        story.click()
    else:
        page.goto(BASE + "/stories/story-7c41e9a2")
    page.wait_for_load_state("networkidle")
    hold(page, 205, "story")
    nav(page, "Workers")
    hold(page, 222, "workers")
    page.get_by_text("Developer 1").first.click()
    page.wait_for_load_state("networkidle")
    hold(page, 250, "worker-detail")
    nav(page, "Journeys")
    page.get_by_text(re.compile("Water reminder", re.I)).first.click()
    page.wait_for_load_state("networkidle")
    hold(page, 395, "journey-ci")               # PR, CI run 1 fails at 385
    nav(page, "Attention")
    hold(page, 420, "attention")
    btn = page.get_by_text("Open journey").first
    if btn.count():
        btn.click()
        page.wait_for_load_state("networkidle")
    else:
        problems.append("no 'Open journey' on Attention")
        page.goto(BASE + "/journeys/story-7c41e9a2")
    hold(page, 470, "journey-fix")              # fix task, CI run 2
    scroll_to(page, "#repo-panel", "end")
    hold(page, 492, "journey-fix-repo")
    top(page)
    hold(page, 565, "journey-deploy")           # deploy → verifying
    nav(page, "Atlas")
    hold(page, 582, "atlas")
    w = page.get_by_text("Water Reminder Bot").first
    if w.count():
        w.click()
        time.sleep(1)
    hold(page, 602, "atlas-passport")
    snap(page, "atlas-passport-final")
    time.sleep(3)
    nav(page, "Journeys")
    time.sleep(3)
    snap(page, "journeys-final")
    length = time.time() - t0
    video = page.video.path()
    ctx.close()
    dst = OUT / "scene1-final-1x.webm"
    shutil.move(video, dst)
    shutil.rmtree(OUT / "raw", ignore_errors=True)
    log.append(f"video: {dst} ({length:.0f}s wall)")
    browser.close()
    ctl("reset")

print("\n".join(log))
print("PROBLEMS:" if problems else "no problems", *sorted(set(problems)), sep="\n  ")
