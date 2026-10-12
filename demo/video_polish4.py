"""Polish task 4 check: short video of scene 1 on Journey and story pages (1920x1080, accelerated).
Records demo/shots/polish-4/scene1-polish4.webm, then:
  - asserts no raw ids ([0-9a-f]{8}-, story-hex, wkr-) in visible text along the way;
  - measures layout shift (CLS) on each page segment;
  - with the scene frozen, counts DOM class/style mutations over several polls (should be ~0 —
    unchanged data must not re-flash or re-animate anything).
Run: uv run --with playwright python demo/video_polish4.py [base_url]"""
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent / "shots" / "polish-4"
OUT.mkdir(parents=True, exist_ok=True)
STORY = "story-7c41e9a2"
J, S = f"/journeys/{STORY}", f"/stories/{STORY}"
ID_RX = [re.compile(r"[0-9a-f]{8}-"), re.compile(r"story-[0-9a-f]+"), re.compile(r"wkr-|run-eng|ci-run-")]
SPEED = 4

CLS_JS = """(() => { window.__cls = 0; new PerformanceObserver((l) => { for (const e of l.getEntries())
  if (!e.hadRecentInput) window.__cls += e.value }).observe({type: 'layout-shift', buffered: false}); })()"""
MUT_JS = """(() => { window.__muts = []; new MutationObserver((ms) => { for (const m of ms) if (m.type === 'attributes')
  window.__muts.push((m.target.className && m.target.className.baseVal === undefined ? String(m.target.className).slice(0, 60) : m.target.tagName) + ' ' + m.attributeName) })
  .observe(document.body, {subtree: true, attributes: true, attributeFilter: ['class', 'style']}); })()"""


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST" if cmd != "state" else "GET")
    return json.load(urllib.request.urlopen(req))


problems: list[str] = []
log: list[str] = []


def scan(page, label):
    text = page.inner_text("body")
    for rx in ID_RX:
        hits = rx.findall(text)
        if hits:
            problems.append(f"{label}: {rx.pattern} → {hits[:3]}")


def segment(page, path, seconds, label, scroll=False):
    page.goto(BASE + path)
    page.wait_for_load_state("networkidle")
    if scroll:
        page.locator("[aria-labelledby=task-board]").scroll_into_view_if_needed()
    page.evaluate(CLS_JS)
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(min(5, seconds - (time.time() - t0)))
        scan(page, f"{label} scene={ctl('state')['scene_second']:.0f}")
    cls = page.evaluate("window.__cls")
    log.append(f"{label}: {seconds}s, CLS={cls:.4f}")
    if cls > 0.05:
        problems.append(f"{label}: layout shift {cls:.3f}")


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                              record_video_dir=str(OUT / "raw"), record_video_size={"width": 1920, "height": 1080})
    page = ctx.new_page()
    ctl("reset")
    ctl(f"start?speed={SPEED}&at=22")
    t_start = time.time()
    segment(page, J, 34, "journey: architect → tasks land → build starts")      # scene ~22 → 160
    segment(page, S, 18, "story: board mid-build", scroll=True)                 # → ~235
    ctl("seek?at=372")
    segment(page, J, 26, "journey: PR in CI → CI fails → fix task")             # 372 → ~476
    ctl("seek?at=560")
    segment(page, S, 12, "story: everything done", scroll=True)                 # 560 → ~600
    video_len = time.time() - t_start
    video = page.video.path()
    ctx.close()
    dst = OUT / "scene1-polish4.webm"
    shutil.move(video, dst)
    shutil.rmtree(OUT / "raw", ignore_errors=True)
    log.append(f"video: {dst} (~{video_len:.0f}s)")

    # Frozen scene: data does not move, so nothing on screen may re-flash or re-animate.
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    for path, at in ((J, 200), (S, 200), (J, 420)):
        ctl("reset")
        ctl(f"start?speed=0.000001&at={at}")
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.evaluate(MUT_JS)
        page.evaluate("window.__anims0 = document.getAnimations().filter(a => a.effect && a.effect.getTiming().iterations !== Infinity).length")
        time.sleep(6)  # four polls
        muts = page.evaluate("window.__muts")
        log.append(f"frozen {path} @{at}: {len(muts)} class/style mutations over 6s {muts[:5]}")
        if muts:
            problems.append(f"frozen {path} @{at}: {len(muts)} mutations")
    browser.close()
    ctl("reset")

print("\n".join(log))
print("PROBLEMS:" if problems else "no problems", *problems, sep="\n  ")
