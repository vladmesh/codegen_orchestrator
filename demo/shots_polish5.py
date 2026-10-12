"""Polish task 5 check: live log, CI failure/fix in the log, growing repository tree + diffs.
Frames (1920x1080) and a ~45 s video into demo/shots/polish-5/, plus:
  - no raw ids ([0-9a-f]{8}-, story-hex, wkr-, run ids) in visible text;
  - no horizontal page scroll; the log follows its tail;
  - frozen scene → no class/style mutations (new panels do not re-render on unchanged polls).
Run: uv run --with playwright python demo/shots_polish5.py [base_url]"""
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
OUT = Path(__file__).resolve().parent / "shots" / "polish-5"
OUT.mkdir(parents=True, exist_ok=True)
STORY = "story-7c41e9a2"
J, S = f"/journeys/{STORY}", f"/stories/{STORY}"
FIX_TASK = "/tasks/a1c3e5f7-6666-4a2b-9c01-000000000006"
SCHED_TASK = "/tasks/a1c3e5f7-1111-4a2b-9c01-000000000001"
ID_RX = [re.compile(r"[0-9a-f]{8}-"), re.compile(r"story-[0-9a-f]+"), re.compile(r"wkr-|run-eng|ci-run-")]
MUT_JS = """(() => { window.__muts = []; new MutationObserver((ms) => { for (const m of ms) if (m.type === 'attributes')
  window.__muts.push((m.target.className && m.target.className.baseVal === undefined ? String(m.target.className).slice(0, 60) : m.target.tagName) + ' ' + m.attributeName) })
  .observe(document.body, {subtree: true, attributes: true, attributeFilter: ['class', 'style']}); })()"""

problems: list[str] = []
log: list[str] = []


def ctl(cmd: str) -> dict:
    req = urllib.request.Request(f"{BASE}/demo/{cmd}", method="POST" if cmd != "state" else "GET")
    return json.load(urllib.request.urlopen(req))


def scene() -> float:
    return ctl("state")["scene_second"]


def check(page, label):
    text = page.inner_text("body")
    for rx in ID_RX:
        hits = rx.findall(text)
        if hits:
            problems.append(f"{label}: {rx.pattern} → {hits[:3]}")
    sw = page.evaluate("document.documentElement.scrollWidth")
    if sw > 1920:
        problems.append(f"{label}: horizontal scroll {sw}px")
    tail = page.evaluate("""(() => { const b = document.querySelector('[role=log]'); if (!b) return null;
      return b.scrollHeight - b.scrollTop - b.clientHeight })()""")
    if tail is not None and tail > 60:
        problems.append(f"{label}: log not at tail ({tail:.0f}px)")
    return text


def frame(page, name, label):
    text = check(page, label)
    page.screenshot(path=str(OUT / name))
    log.append(f"{name}: scene={scene():.0f} {label}")
    return text


def wait_scene(at: float):
    while scene() < at:
        time.sleep(0.2)


def show_repo(page):
    page.evaluate("document.querySelector('#repo-panel').closest('section').scrollIntoView({block: 'end'})")
    time.sleep(0.6)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)

    # --- frames, each from its own scene second
    ctl("reset")
    ctl("start?speed=4&at=20")
    page.goto(BASE + J)
    page.wait_for_load_state("networkidle")
    wait_scene(27)
    frame(page, "01-journey-scaffold-log.png", "scaffolder writes the repo")
    wait_scene(84)
    frame(page, "02-journey-log-dev-start.png", "developers 1 and 2 coding, scheduler test red then green")
    wait_scene(98)
    show_repo(page)
    frame(page, "03-repo-tree-diff-early.png", "tree grows, latest diff")
    ctl("seek?at=255")
    time.sleep(1.2)
    page.goto(BASE + SCHED_TASK)
    page.wait_for_load_state("networkidle")
    time.sleep(0.8)
    frame(page, "04-task-scheduler-log-and-changes.png", "task page: developer log + task diff")

    ctl("seek?at=352")
    page.goto(BASE + J)
    page.wait_for_load_state("networkidle")
    wait_scene(372)
    t = frame(page, "05-journey-ci-run1-failed-log.png", "CI run 1: test_streak_resets_at_midnight fails")
    if "test_streak_resets_at_midnight" not in t or "assert 1 == 0" not in t:
        problems.append("CI failure not visible in the log at 372")
    wait_scene(392)
    frame(page, "06-journey-ci-failed-fix-task.png", "CI failed, fix task created")

    ctl("seek?at=446")
    time.sleep(1.2)
    page.goto(BASE + FIX_TASK)
    page.wait_for_load_state("networkidle")
    time.sleep(0.8)
    frame(page, "07-task-fix-streak-diff.png", "fix task: local_day diff")

    ctl("seek?at=476")
    page.goto(BASE + J)
    page.wait_for_load_state("networkidle")
    wait_scene(484)
    t = frame(page, "08-journey-ci-run2-passed.png", "CI run 2 passed, merged")
    if "48 passed" not in t:
        problems.append("CI run 2 pass not visible at 484")
    show_repo(page)
    frame(page, "09-repo-streak-fix-diff.png", "repo: streak.py fix diff")

    ctl("seek?at=599")
    page.goto(BASE + J)
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    frame(page, "10-journey-final.png", "live, QA 9/9")
    page.goto(BASE + S)
    page.wait_for_load_state("networkidle")
    time.sleep(1.5)
    frame(page, "11-story-final.png", "story page final")
    show_repo(page)
    frame(page, "12-story-final-repo.png", "final repository")

    # --- frozen scene: unchanged polls must not touch the DOM
    for path, at in ((J, 100), (J, 372), (FIX_TASK, 440)):
        ctl("reset")
        ctl(f"start?speed=0.000001&at={at}")
        page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
        page.evaluate(MUT_JS)
        time.sleep(6)
        muts = page.evaluate("window.__muts")
        log.append(f"frozen {path.split('/')[1]} @{at}: {len(muts)} mutations {muts[:4]}")
        if muts:
            problems.append(f"frozen {path} @{at}: {len(muts)} mutations")
    page.close()

    # --- video ~45 s: log during the build, repo, CI failure, fix, CI run 2, final
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                              record_video_dir=str(OUT / "raw"), record_video_size={"width": 1920, "height": 1080})
    page = ctx.new_page()
    ctl("reset")
    ctl("start?speed=6&at=46")
    t0 = time.time()
    page.goto(BASE + J)
    page.wait_for_load_state("networkidle")
    page.evaluate("window.scrollTo({top: 400, behavior: 'smooth'})")
    wait_scene(100)
    page.evaluate("document.querySelector('#repo-panel').closest('section').scrollIntoView({block: 'end', behavior: 'smooth'})")
    wait_scene(150)
    page.evaluate("window.scrollTo({top: 400, behavior: 'smooth'})")
    wait_scene(170)
    ctl("seek?at=350")
    wait_scene(394)
    check(page, "video CI failure")
    ctl("seek?at=430")
    page.goto(BASE + FIX_TASK)
    page.wait_for_load_state("networkidle")
    wait_scene(452)
    page.goto(BASE + J)
    page.wait_for_load_state("networkidle")
    page.evaluate("window.scrollTo({top: 400})")
    wait_scene(490)
    ctl("seek?at=584")
    wait_scene(600)
    time.sleep(2)
    page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
    time.sleep(1.5)
    length = time.time() - t0
    video = page.video.path()
    ctx.close()
    dst = OUT / "scene1-polish5.webm"
    shutil.move(video, dst)
    shutil.rmtree(OUT / "raw", ignore_errors=True)
    log.append(f"video: {dst} (~{length:.0f}s)")
    browser.close()
    ctl("reset")

print("\n".join(log))
print("PROBLEMS:" if problems else "no problems", *problems, sep="\n  ")
