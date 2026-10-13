"""Scene 1 live material: what the agents print and which files they write, second by second.

One script (SCRIPT) drives three views so they never disagree:
  - the live log (story-wide, per task, per worker);
  - the repository tree and file contents at any scene second;
  - the diff of a task or of PR #1 against the scaffold.

Times are scene seconds and follow scene1.timeline.json (task in_dev / in_ci / done, CI runs,
deploy, QA). Commit stats ("4 files changed, …") are computed from the real diffs.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from scene1_repo import FILES, failing_line

WHO = {  # log source → short tag shown in the log
    "architect": "architect", "scaffolder": "scaffold", "dev1": "dev 1", "dev2": "dev 2", "dev3": "dev 3",
    "ci": "ci", "deployer": "deploy", "qa": "qa", "brimos": "brimos",
}
WORKER_OF = {"dev1": "wkr-3f9c2a71d0e4", "dev2": "wkr-b81e04c6a925", "dev3": "wkr-5d07ae19c3b2", "qa": "wkr-qa-e2a4c8f01b"}
SCAFFOLD_DONE = 29


@dataclass
class Line:
    t: float
    who: str
    kind: str  # cmd | out | ok | err | info | file | step
    text: str
    task: int | None = None  # 1..4, task the line belongs to
    file: tuple[str, int] | None = None  # (path, version index) written at this moment
    n: int = 0


def _task_script(task: int, who: str, start: float, end: float, steps: list[tuple]) -> list[Line]:
    """steps: (fraction of [start, end], kind, text[, (path, version)])."""
    out = []
    for step in steps:
        frac, kind, text = step[:3]
        f = step[3] if len(step) > 3 else None
        out.append(Line(round(start + (end - start) * frac, 1), who, kind, text, task, f))
    return out


def _ci_task(task: int, branch: str, start: float, end: float, tests: str) -> list[Line]:
    span = end - start
    return [
        Line(start + 0.5, "ci", "step", f"CI · {branch}", task),
        Line(start + span * 0.3, "ci", "cmd", "ruff check .", task),
        Line(start + span * 0.4, "ci", "out", "All checks passed!", task),
        Line(start + span * 0.5, "ci", "cmd", "pytest -q", task),
        Line(start + span * 0.85, "ci", "ok", tests, task),
        Line(end, "brimos", "ok", f"Merged {branch} into main", task),
    ]


def _build_script() -> list[Line]:
    L: list[Line] = []
    add = L.append

    # --- planning (0..49)
    for t, who, kind, text, f in [
        (1, "architect", "step", "Reading the confirmed brief", None),
        (4, "architect", "out", "Request: reminders 9:00–21:00 every 2 h, daily goal, weekly summary, streak", None),
        (9, "architect", "info", "Stack: python 3.12, aiogram 3, APScheduler, sqlite", None),
        (14, "architect", "info", "Open question resolved: streak day = user's local day", None),
        (19, "architect", "ok", "7 requirements written — see the list on the journey", None),
        (20, "scaffolder", "cmd", "gh repo create brimos-products/water-reminder-bot --private", None),
        (21, "scaffolder", "out", "Created repository brimos-products/water-reminder-bot", None),
        (22, "scaffolder", "file", "Created pyproject.toml", ("pyproject.toml", 0)),
        (22.5, "scaffolder", "file", "Created README.md", ("README.md", 0)),
        (23, "scaffolder", "file", "Created .gitignore", (".gitignore", 0)),
        (23.5, "scaffolder", "file", "Created Dockerfile", ("Dockerfile", 0)),
        (24, "scaffolder", "file", "Created .github/workflows/ci.yml", (".github/workflows/ci.yml", 0)),
        (25, "scaffolder", "file", "Created bot/__init__.py", ("bot/__init__.py", 0)),
        (25.5, "scaffolder", "file", "Created bot/config.py", ("bot/config.py", 0)),
        (26, "scaffolder", "file", "Created bot/main.py", ("bot/main.py", 0)),
        (27, "scaffolder", "cmd", 'git commit -m "Scaffold: aiogram bot, CI, Dockerfile"', None),
        (28, "scaffolder", "out", "{stat:scaffold}", None),
        (29, "scaffolder", "ok", "Pushed main", None),
        (31, "architect", "cmd", "catalog search: reminder, streak, habit", None),
        (34, "architect", "out", "no package matches — new code", None),
        (36, "architect", "info", "Task 1: Reminder scheduler", None),
        (37, "architect", "info", "Task 2: Glasses, daily goal and streak", None),
        (39, "architect", "info", "Task 3: Commands, /stats and weekly summary", None),
        (45, "architect", "ok", "3 tasks in 2 lanes, handing over to developers", None),
    ]:
        add(Line(t, who, kind, text, None, f))

    # --- task 1: reminder scheduler (Developer 1, 52 → 105 → 120)
    L += _task_script(1, "dev1", 52, 105, [
        (0.00, "step", "Task: Reminder scheduler (every 2 h, 9:00–21:00, user time zone)"),
        (0.03, "cmd", "git checkout -b task/reminder-scheduler"),
        (0.07, "cmd", "cat bot/main.py bot/config.py pyproject.toml"),
        (0.12, "info", "One cron job per user: hours 9–21 every 2 h, in the user's ZoneInfo"),
        (0.18, "file", "Created bot/db.py", ("bot/db.py", 0)),
        (0.27, "file", "Created bot/scheduler.py", ("bot/scheduler.py", 0)),
        (0.36, "file", "Created bot/handlers/reminders.py", ("bot/handlers/reminders.py", 0)),
        (0.37, "file", "Created bot/handlers/__init__.py", ("bot/handlers/__init__.py", 0)),
        (0.44, "file", "Edited bot/main.py", ("bot/main.py", 1)),
        (0.52, "file", "Created tests/test_scheduler.py", ("tests/test_scheduler.py", 0)),
        (0.58, "cmd", "pytest -q tests/test_scheduler.py"),
        (0.62, "err", "FAILED tests/test_scheduler.py::test_no_reminders_outside_9_to_21[21]"),
        (0.63, "err", "FAILED tests/test_scheduler.py::test_every_two_hours_from_9_to_21 — no 21:00"),
        (0.64, "err", "2 failed, 27 passed in 0.38s"),
        (0.68, "info", "Cron hours 9-20/2 stop at 19:00; the range must end at 21"),
        (0.72, "file", "Edited bot/scheduler.py", ("bot/scheduler.py", 1)),
        (0.78, "cmd", "pytest -q tests/test_scheduler.py"),
        (0.82, "ok", "29 passed in 0.41s"),
        (0.86, "cmd", "ruff check ."),
        (0.88, "out", "All checks passed!"),
        (0.91, "cmd", 'git commit -m "Reminder scheduler: every 2 h, 9:00–21:00, user tz"'),
        (0.93, "out", "{stat:1}"),
        (0.96, "cmd", "git push -u origin task/reminder-scheduler"),
        (1.00, "ok", "Pushed, CI started"),
    ])
    L += _ci_task(1, "task/reminder-scheduler", 105, 120, "29 passed in 0.52s")

    # --- task 2: glasses, goal and streak (Developer 2, 55 → 180 → 195)
    L += _task_script(2, "dev2", 55, 118, [
        (0.00, "step", "Task: Glasses, daily goal (/goal) and streak"),
        (0.03, "cmd", "git checkout -b task/glasses-and-streak"),
        (0.08, "cmd", "git pull origin task/reminder-scheduler -- bot/db.py"),
        (0.15, "info", "Glasses are rows per day; goal lives on the user, default 8"),
        (0.30, "file", "Edited bot/db.py — goal must be 1–30", ("bot/db.py", 1)),
        (0.40, "file", "Created bot/handlers/goal.py", ("bot/handlers/goal.py", 0)),
        (0.48, "file", "Created tests/conftest.py", ("tests/conftest.py", 0)),
        (0.55, "file", "Created tests/test_goal.py", ("tests/test_goal.py", 0)),
        (0.62, "file", "Edited bot/main.py — register /goal", ("bot/main.py", 2)),
        (0.70, "cmd", "pytest -q tests/test_goal.py"),
        (0.76, "ok", "5 passed in 0.29s"),
        (0.86, "cmd", 'git commit -m "Glass counter and /goal (1-30 glasses)"'),
        (1.00, "info", "Goal works; next: the streak on top of the daily totals"),
    ])
    L += _task_script(2, "dev2", 118, 180, [
        (0.10, "info", "Streak +1 when the goal is reached on the next day, 0 after a missed day"),
        (0.24, "file", "Edited bot/db.py — streaks table", ("bot/db.py", 2)),
        (0.40, "file", "Created bot/streak.py", ("bot/streak.py", 0)),
        (0.55, "file", "Created tests/test_streak.py", ("tests/test_streak.py", 0)),
        (0.66, "cmd", "pytest -q"),
        (0.72, "ok", "37 passed in 0.70s"),
        (0.80, "cmd", "ruff check ."),
        (0.82, "out", "All checks passed!"),
        (0.88, "cmd", 'git commit -m "Daily streak with best streak"'),
        (0.91, "out", "{stat:2}"),
        (0.96, "cmd", "git push -u origin task/glasses-and-streak"),
        (1.00, "ok", "Pushed, CI started"),
    ])
    L += _ci_task(2, "task/glasses-and-streak", 180, 195, "37 passed in 0.70s")

    # --- task 3: weekly summary, then commands (Developer 1, 125 → 268 → 285)
    L += _task_script(3, "dev1", 125, 195, [
        (0.00, "step", "Task: Commands, /stats and weekly summary"),
        (0.03, "cmd", "git checkout -b task/commands-and-summary main"),
        (0.10, "info", "Sunday 20:00 in the user's time zone; totals from the last 7 days"),
        (0.25, "file", "Created bot/summary.py", ("bot/summary.py", 0)),
        (0.40, "file", "Edited bot/db.py — last_7_days()", ("bot/db.py", 3)),
        (0.52, "file", "Created tests/test_summary.py", ("tests/test_summary.py", 0)),
        (0.62, "cmd", "pytest -q tests/test_summary.py"),
        (0.66, "err", "FAILED test_total_glasses — expected '46 glasses'"),
        (0.70, "info", "Fixture adds up to 46, the assert used 48; fixed the test data"),
        (0.76, "cmd", "pytest -q tests/test_summary.py"),
        (0.80, "ok", "3 passed in 0.12s"),
        (0.88, "cmd", 'git commit -m "Weekly summary on Sundays at 20:00"'),
        (1.00, "info", "Summary done; next: /start, /stats and the +1 glass button"),
    ])
    L += _task_script(3, "dev1", 195, 268, [
        (0.12, "info", "/start registers the user and schedules reminders; +1 button closes the day"),
        (0.25, "file", "Created bot/handlers/commands.py", ("bot/handlers/commands.py", 0)),
        (0.38, "file", "Created tests/fakes.py", ("tests/fakes.py", 0)),
        (0.44, "file", "Edited tests/conftest.py — fake scheduler", ("tests/conftest.py", 1)),
        (0.52, "file", "Created tests/test_commands.py", ("tests/test_commands.py", 0)),
        (0.60, "file", "Edited bot/main.py — register commands", ("bot/main.py", 3)),
        (0.68, "cmd", "pytest -q"),
        (0.75, "ok", "42 passed in 0.88s"),
        (0.80, "cmd", "ruff check ."),
        (0.82, "out", "All checks passed!"),
        (0.88, "cmd", 'git commit -m "/start, /stats and the +1 glass button"'),
        (0.91, "out", "{stat:3}"),
        (0.96, "cmd", "git push -u origin task/commands-and-summary"),
        (1.00, "ok", "Pushed, CI started"),
    ])
    L += _ci_task(3, "task/commands-and-summary", 268, 285, "42 passed in 0.93s")

    # --- PR #1 and CI run 1 (292 → 385, fails)
    fl = failing_line()
    for t, who, kind, text, f in [
        (287, "architect", "file", "Added tests/test_acceptance.py — 6 checks from the acceptance criteria",
         ("tests/test_acceptance.py", 0)),
        (290, "brimos", "step", "Opened pull request #1: Water reminder bot with daily streak", None),
        (292, "ci", "step", "CI run 1 · PR #1", None),
        (296, "ci", "cmd", "pip install -e '.[test]'", None),
        (312, "ci", "out", "Successfully installed aiogram-3.13.1 aiosqlite-0.20.0 apscheduler-3.10.4 pytest-8.3.3 …", None),
        (318, "ci", "cmd", "ruff check .", None),
        (321, "ci", "out", "All checks passed!", None),
        (326, "ci", "cmd", "pytest -q", None),
        (338, "ci", "out", "...F.............................. [ 83%]", None),
        (352, "ci", "out", "......                             [100%]", None),
        (360, "ci", "err", "FAILED tests/test_acceptance.py::test_streak_resets_at_midnight", None),
        (362, "ci", "out", "    user = await make_user(store, \"Asia/Tokyo\")", None),
        (363, "ci", "out", "    await log_glasses(store, user, 8, at=utc(2026, 10, 11, 13, 0))", None),
        (364, "ci", "out", ">   assert await current_streak(store, user, now=utc(2026, 10, 12, 23, 0)) == 0, \\", None),
        (366, "ci", "err", "E   AssertionError: streak should reset after a missed local day", None),
        (367, "ci", "err", "E   assert 1 == 0", None),
        (369, "ci", "out", f"tests/test_acceptance.py:{fl}: AssertionError", None),
        (384, "ci", "err", "1 failed, 47 passed in 3.12s", None),
        (385, "ci", "err", "CI run 1 failed", None),
        (386, "architect", "info", "Cause: streak.py takes now.date() — UTC, not the user's day", None),
        (386.5, "architect", "step", "Fix task: streak resets at the user's local midnight", None),
    ]:
        add(Line(t, who, kind, text, None, f))

    # --- task 4: fix (Developer 3, 387 → 450, then CI run 2 → 482)
    L += _task_script(4, "dev3", 387, 450, [
        (0.00, "step", "Task: Fix: streak resets at the user's local midnight"),
        (0.04, "cmd", "git checkout fix/streak-local-midnight"),
        (0.10, "cmd", "pytest -q tests/test_acceptance.py -k midnight"),
        (0.16, "err", "1 failed in 0.21s"),
        (0.24, "info", "08:00 Oct 13 in Tokyo is still Oct 12 in UTC — the missed day is not seen"),
        (0.34, "info", "Day boundary goes through local_day(user, now) with the user's ZoneInfo"),
        (0.46, "file", "Edited bot/streak.py", ("bot/streak.py", 1)),
        (0.58, "cmd", "pytest -q tests/test_acceptance.py -k midnight"),
        (0.63, "ok", "1 passed in 0.19s"),
        (0.70, "cmd", "pytest -q"),
        (0.78, "ok", "48 passed in 2.98s"),
        (0.86, "cmd", 'git commit -m "Streak day ends at the user\'s local midnight"'),
        (0.89, "out", "{stat:4}"),
        (0.95, "cmd", "git push origin fix/streak-local-midnight"),
        (1.00, "ok", "Pushed to PR #1"),
    ])
    for t, who, kind, text in [
        (452, "ci", "step", "CI run 2 · PR #1"),
        (454, "ci", "cmd", "pip install -e '.[test]'"),
        (459, "ci", "out", "Using cached wheels, 9 packages installed"),
        (461, "ci", "cmd", "ruff check ."),
        (462, "ci", "out", "All checks passed!"),
        (464, "ci", "cmd", "pytest -q"),
        (470, "ci", "out", ".................................. [ 83%]"),
        (476, "ci", "out", "......                             [100%]"),
        (481, "ci", "ok", "48 passed in 3.05s"),
        (482, "ci", "ok", "CI run 2 passed"),
        (488, "brimos", "ok", "Merged pull request #1 into main"),
        # deploy
        (492, "deployer", "step", "Deploy water-reminder-bot"),
        (495, "deployer", "info", "Server: edge-eu-2, 256 MB reserved, port 8031"),
        (500, "deployer", "cmd", "docker build -t water-reminder-bot ."),
        (522, "deployer", "out", "Successfully built water-reminder-bot"),
        (528, "deployer", "info", "Secret TELEGRAM_BOT_TOKEN attached"),
        (534, "deployer", "cmd", "docker run -d --name water-reminder-bot -v bot-data:/app/data …"),
        (545, "deployer", "out", "Container running"),
        (548, "deployer", "ok", "Health: 200 OK · 84 ms"),
        # QA on the live bot
        (555, "qa", "step", "Checking the live bot @brimos_demo_first_part_bot"),
        (560, "qa", "ok", "/start → reply in 0.6 s, reminders scheduled"),
        (565, "qa", "ok", "+1 glass → 1/8 today"),
        (570, "qa", "ok", "/goal 6 → Daily goal set to 6 glasses"),
        (575, "qa", "ok", "6 glasses → streak 1"),
        (580, "qa", "ok", "Next reminder at 11:00 local time"),
        (585, "qa", "ok", "/stats → Today 6/6, Streak 1, Best 1"),
        (590, "qa", "ok", "Weekly summary job: Sunday 20:00"),
        (594, "qa", "ok", "9 / 9 checks passed"),
        (598, "brimos", "ok", "Live: t.me/brimos_demo_first_part_bot"),
    ]:
        add(Line(t, who, kind, text))

    L.sort(key=lambda x: x.t)
    for i, line in enumerate(L):
        line.n = i + 1
    return L


SCRIPT = _build_script()
TASK_SPAN = {1: (52, 120), 2: (55, 195), 3: (125, 285), 4: (387, 482)}


# ---------------------------------------------------------------- repository state
def file_events() -> list[Line]:
    return [x for x in SCRIPT if x.file]


def repo_at(s: float, upto_task: int | None = None) -> dict[str, tuple[int, float, int | None]]:
    """path → (version, time written, task) for every file written by scene second s."""
    state: dict[str, tuple[int, float, int | None]] = {}
    for e in file_events():
        if e.t > s:
            break
        state[e.file[0]] = (e.file[1], e.t, e.task)
    return state


def content(path: str, ver: int) -> str:
    return FILES[path][ver]


def _diff(old: str | None, new: str) -> tuple[list[str], int, int]:
    a = old.splitlines() if old is not None else []
    b = new.splitlines()
    lines = list(difflib.unified_diff(a, b, lineterm="", n=2))[2:]  # drop ---/+++ headers
    adds = sum(1 for x in lines if x.startswith("+"))
    dels = sum(1 for x in lines if x.startswith("-"))
    return lines, adds, dels


def changes(s: float, base_t: float, task: int | None = None) -> list[dict]:
    """Files changed after base_t up to s (only by `task` if given), newest change first."""
    base = repo_at(base_t)
    head = {}
    for e in file_events():
        if e.t > s:
            break
        if e.t <= base_t or (task is not None and e.task != task):
            continue
        head[e.file[0]] = (e.file[1], e.t)
    out = []
    for path, (ver, t) in head.items():
        old = content(path, base[path][0]) if path in base else None
        lines, adds, dels = _diff(old, content(path, ver))
        out.append({"path": path, "status": "modified" if old is not None else "added",
                    "additions": adds, "deletions": dels, "patch": "\n".join(lines), "t": t})
    out.sort(key=lambda f: -f["t"])
    return out


def tree(s: float) -> list[dict]:
    state = repo_at(s)
    dirs = set()
    for path in state:
        parts = path.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    rows = [{"path": d, "is_dir": True, "size": 0} for d in sorted(dirs)]
    rows += [{"path": p, "is_dir": False, "size": len(content(p, v).encode())} for p, (v, _, _) in sorted(state.items())]
    return rows


def _stat(key: str) -> str:
    if key == "scaffold":
        files = changes(SCAFFOLD_DONE, -1)
    else:
        n = int(key)
        files = changes(TASK_SPAN[n][1], TASK_SPAN[n][0] - 0.01, task=n)
    adds = sum(f["additions"] for f in files)
    dels = sum(f["deletions"] for f in files)
    plural = lambda n, w: f"{n} {w}{'s' if n != 1 else ''}"  # noqa: E731
    parts = [f"{plural(len(files), 'file')} changed", f"{plural(adds, 'insertion')}(+)"]
    if dels:
        parts.append(f"{plural(dels, 'deletion')}(-)")
    return ", ".join(parts)


for _line in SCRIPT:
    if _line.text.startswith("{stat:"):
        _line.text = _stat(_line.text[6:-1])


# ---------------------------------------------------------------- log views
def log(s: float, task: int | None = None, who: str | None = None, limit: int = 400) -> list[Line]:
    rows = [x for x in SCRIPT if x.t <= s and (task is None or x.task == task) and (who is None or x.who == who)]
    return rows[-limit:]


ANSI = {"cmd": "\x1b[36m$ ", "ok": "\x1b[32m", "err": "\x1b[31m", "info": "\x1b[33m", "file": "\x1b[35m",
        "step": "\x1b[1m", "out": ""}


def worker_log_text(worker_id: str, s: float, tail: int) -> str:
    who = next((k for k, v in WORKER_OF.items() if v == worker_id), None)
    if who is None:
        return ""
    rows = log(s, who=who, limit=tail)
    return "\n".join(f"{ANSI[x.kind]}{x.text}\x1b[0m" for x in rows)


# ---------------------------------------------------------------- dashboard payload
NAMES = {"architect": "Architect", "scaffolder": "Scaffolder", "dev1": "Developer 1", "dev2": "Developer 2",
         "dev3": "Developer 3", "ci": "GitHub CI", "deployer": "Deployer", "qa": "QA", "brimos": "Brimos"}
RECENT = 12  # scene seconds a file stays marked as just written


def _last_write(s: float, path: str | None = None, task: int | None = None) -> Line | None:
    last = None
    for e in file_events():
        if e.t > s:
            break
        if (path is None or e.file[0] == path) and (task is None or e.task == task):
            last = e
    return last


def _file_change(e: Line) -> dict:
    """Diff of one write against the previous version of the file."""
    path, ver = e.file
    old = content(path, ver - 1) if ver > 0 else None
    lines, adds, dels = _diff(old, content(path, ver))
    return {"path": path, "status": "modified" if old is not None else "added", "additions": adds,
            "deletions": dels, "patch": "\n".join(lines), "author": NAMES[e.who], "t": e.t, "task": e.task}


def live(s: float, ts, task: int | None = None, file: str | None = None, limit: int = 160) -> dict:
    """Everything the Live log / Repository panels need at scene second s. ts(t) → iso timestamp."""
    rows = log(s, task=task, limit=limit)
    lines = [{"n": x.n, "at": ts(x.t), "source": NAMES[x.who], "kind": x.kind, "text": x.text} for x in rows]
    state = repo_at(s)
    base = repo_at(SCAFFOLD_DONE)
    files = []
    for row in tree(s):
        entry = dict(row)
        if not row["is_dir"]:
            ver, t, _ = state[row["path"]]
            if s - t <= RECENT and t > SCAFFOLD_DONE:
                entry["mark"] = "edited" if ver > 0 else "new"
            # +/- against the scaffold, i.e. what the pull request adds
            if t > SCAFFOLD_DONE:
                old = content(row["path"], base[row["path"]][0]) if row["path"] in base else None
                _, entry["additions"], entry["deletions"] = _diff(old, content(row["path"], ver))
        files.append(entry)
    focus = None
    if file and file in state:
        e = _last_write(s, path=file)
        focus = _file_change(e) if e else None
    else:
        e = _last_write(s, task=task)
        focus = _file_change(e) if e and e.t > (SCAFFOLD_DONE if task is None else -1) else None
    if focus:
        focus["at"] = ts(focus.pop("t"))
        focus.pop("task", None)
    task_changes = None
    if task is not None:
        start, end = TASK_SPAN[task]
        task_changes = [{k: v for k, v in c.items() if k != "t"} for c in changes(min(s, end), start - 0.01, task=task)]
    return {"lines": lines, "files": files, "focus": focus, "task_changes": task_changes}
