"""Scene 2 live material: what the agents print and which files they write, second by second.

Times are scene seconds and follow scene2.timeline.json. The architect finds tg-channels in the
catalog and the platform service tg-reader, so the repo holds only the new logic (2 tasks).
At the end the ranking code goes to the catalog as tg-post-ranking 0.1.0.
"""
from __future__ import annotations

from feed_engine import Feed, Line, ci_task, task_script
from scene2_repo import FILES

WORKER_OF = {"dev1": "wkr-7a1d5c90e3b2", "dev2": "wkr-e40b8f26a1c7", "qa": "wkr-qa-5c3e9a0d17"}
SCAFFOLD_DONE = 37
NAMES = {"architect": "Architect", "scaffolder": "Scaffolder", "dev1": "Developer 1", "dev2": "Developer 2",
         "ci": "GitHub CI", "deployer": "Deployer", "qa": "QA", "brimos": "Brimos"}
TASK_SPAN = {1: (42, 110), 2: (44, 129)}


def _build_script() -> list[Line]:
    L: list[Line] = []
    add = L.append

    # --- planning and catalog (0..40)
    for t, who, kind, text, f in [
        (1, "architect", "step", "Reading the confirmed brief", None),
        (4, "architect", "out", "Request: weekly digest of 3 competitors' Telegram channels, top posts by views", None),
        (8, "architect", "info", "Stack: python 3.12, aiogram 3, APScheduler, sqlite", None),
        (11, "architect", "ok", "6 requirements written — see the list on the journey", None),
        (14, "architect", "cmd", "catalog search: telegram channel posts, views", None),
        (18, "architect", "out", "tg-channels 0.1.2 — reads posts, views and links of public channels", None),
        (20, "architect", "out", "  used by: Morning Tech Digest", None),
        (22, "architect", "ok", "Reuse tg-channels 0.1.2: covers requirement 2", None),
        (23, "architect", "cmd", "platform services: telegram reading", None),
        (24, "architect", "ok", "tg-reader is up: shared reader, no Telegram account of the user needed", None),
        (26, "scaffolder", "step", "Scaffold with the catalog package", None),
        (27, "scaffolder", "cmd", "gh repo create brimos-products/competitor-digest-bot --private", None),
        (27.5, "scaffolder", "out", "Created repository brimos-products/competitor-digest-bot", None),
        (28, "scaffolder", "file", "Created pyproject.toml — tg-channels==0.1.2", ("pyproject.toml", 0)),
        (28.5, "scaffolder", "file", "Created README.md", ("README.md", 0)),
        (29, "scaffolder", "file", "Created .gitignore", (".gitignore", 0)),
        (29.5, "scaffolder", "file", "Created Dockerfile", ("Dockerfile", 0)),
        (30, "scaffolder", "file", "Created .github/workflows/ci.yml", (".github/workflows/ci.yml", 0)),
        (30.5, "architect", "info", "Task 1: Rank posts by views and flag the ones that took off", None),
        (31, "scaffolder", "cmd", "pip install tg-channels==0.1.2", None),
        (32, "scaffolder", "out", "Successfully installed tg-channels-0.1.2", None),
        (32.5, "architect", "info", "Task 2: Monday digest and the /add, /remove commands", None),
        (33, "scaffolder", "ok", "Platform service tg-reader: access granted, TG_READER_URL set at deploy", None),
        (34, "scaffolder", "file", "Created bot/__init__.py", ("bot/__init__.py", 0)),
        (34.3, "scaffolder", "file", "Created bot/config.py", ("bot/config.py", 0)),
        (34.6, "scaffolder", "file", "Created bot/sources.py — posts via tg-channels", ("bot/sources.py", 0)),
        (35, "scaffolder", "file", "Created bot/main.py", ("bot/main.py", 0)),
        (35.5, "scaffolder", "cmd", 'git commit -m "Scaffold: aiogram bot, tg-channels, CI, Dockerfile"', None),
        (36, "scaffolder", "out", "{stat:scaffold}", None),
        (37, "scaffolder", "ok", "Pushed main", None),
        (38, "architect", "ok", "2 tasks instead of 3: reading channels is already covered", None),
    ]:
        add(Line(t, who, kind, text, None, f))

    # --- task 1: ranking (Developer 1, 42 → 98 → 110)
    L += task_script(1, "dev1", 42, 98, [
        (0.00, "step", "Task: Rank posts by views and flag the ones that took off"),
        (0.03, "cmd", "git checkout -b task/ranking"),
        (0.08, "cmd", "python -c 'import tg_channels; help(tg_channels.Post)'"),
        (0.13, "out", "Post(channel, id, date, text, views, url)"),
        (0.20, "info", "Took off = views at least 3× the channel's weekly median"),
        (0.32, "file", "Created bot/ranking.py", ("bot/ranking.py", 0)),
        (0.45, "file", "Created tests/test_ranking.py", ("tests/test_ranking.py", 0)),
        (0.53, "cmd", "pytest -q tests/test_ranking.py"),
        (0.58, "err", "FAILED test_exactly_three_times_is_flagged — took_off is False"),
        (0.59, "err", "FAILED test_all_zero_views_flags_nothing — 0 > 0 * 3 is fine, 0 >= 0 is not"),
        (0.60, "err", "2 failed, 3 passed in 0.09s"),
        (0.66, "info", "Use >= for the 3× rule and skip channels with a zero median"),
        (0.72, "file", "Edited bot/ranking.py", ("bot/ranking.py", 1)),
        (0.79, "cmd", "pytest -q tests/test_ranking.py"),
        (0.83, "ok", "5 passed in 0.08s"),
        (0.88, "cmd", "ruff check ."),
        (0.90, "out", "All checks passed!"),
        (0.93, "cmd", 'git commit -m "Rank posts by views, flag 3x the weekly median"'),
        (0.95, "out", "{stat:1}"),
        (0.98, "cmd", "git push -u origin task/ranking"),
        (1.00, "ok", "Pushed, CI started"),
    ])
    L += ci_task(1, "task/ranking", 98, 110, "5 passed in 0.31s")

    # --- task 2: digest and commands (Developer 2, 44 → 116 → 129)
    L += task_script(2, "dev2", 44, 116, [
        (0.00, "step", "Task: Monday digest and the /add, /remove commands"),
        (0.03, "cmd", "git checkout -b task/digest-and-commands"),
        (0.08, "info", "Channels per user in sqlite; digest = rank() of each channel's week"),
        (0.18, "file", "Created bot/store.py", ("bot/store.py", 0)),
        (0.30, "file", "Created bot/digest.py", ("bot/digest.py", 0)),
        (0.40, "file", "Created bot/handlers/__init__.py", ("bot/handlers/__init__.py", 0)),
        (0.41, "file", "Created bot/handlers/channels.py", ("bot/handlers/channels.py", 0)),
        (0.52, "file", "Edited bot/main.py — schedule digests, register /add and /remove", ("bot/main.py", 1)),
        (0.60, "file", "Created tests/conftest.py", ("tests/conftest.py", 0)),
        (0.66, "file", "Created tests/test_digest.py", ("tests/test_digest.py", 0)),
        (0.74, "cmd", "pytest -q"),
        (0.80, "ok", "8 passed in 0.42s"),
        (0.85, "cmd", "ruff check ."),
        (0.87, "out", "All checks passed!"),
        (0.91, "cmd", 'git commit -m "Monday 9:00 digest, /add and /remove"'),
        (0.94, "out", "{stat:2}"),
        (0.97, "cmd", "git push -u origin task/digest-and-commands"),
        (1.00, "ok", "Pushed, CI started"),
    ])
    L += ci_task(2, "task/digest-and-commands", 116, 129, "8 passed in 0.47s")

    # --- PR, CI, deploy, QA, catalog
    for t, who, kind, text, f in [
        (130, "architect", "file", "Added tests/test_acceptance.py — 3 checks from the acceptance criteria",
         ("tests/test_acceptance.py", 0)),
        (132, "brimos", "step", "Opened pull request #1: Weekly digest of competitors' Telegram channels", None),
        (134, "ci", "step", "CI run 1 · PR #1", None),
        (137, "ci", "cmd", "pip install -e '.[test]' --extra-index-url https://catalog.brimos.dev/simple", None),
        (144, "ci", "out", "Successfully installed aiogram-3.13.1 apscheduler-3.10.4 tg-channels-0.1.2 …", None),
        (147, "ci", "cmd", "ruff check .", None),
        (148, "ci", "out", "All checks passed!", None),
        (150, "ci", "cmd", "pytest -q", None),
        (155, "ci", "out", "...........                        [100%]", None),
        (159, "ci", "ok", "11 passed in 1.12s", None),
        (160, "ci", "ok", "CI run 1 passed", None),
        (165, "brimos", "ok", "Merged pull request #1 into main", None),
        (168, "deployer", "step", "Deploy competitor-digest-bot", None),
        (172, "deployer", "info", "Server: edge-eu-2, 256 MB reserved, port 8032", None),
        (174, "deployer", "cmd", "docker build -t competitor-digest-bot .", None),
        (178, "architect", "step", "Catalog check: is any new code reusable?", None),
        (182, "architect", "info", "bot/ranking.py imports only tg_channels.Post — no product code", None),
        (180, "deployer", "info", "Secret TELEGRAM_BOT_TOKEN attached", None),
        (181, "deployer", "info", "Platform service tg-reader: TG_READER_URL injected", None),
        (186, "deployer", "out", "Successfully built competitor-digest-bot", None),
        (188, "deployer", "cmd", "docker run -d --name competitor-digest-bot -v digest-data:/app/data …", None),
        (194, "deployer", "out", "Container running", None),
        (197, "deployer", "ok", "Health: 200 OK · 91 ms", None),
        (201, "qa", "step", "Checking the live bot @brimos_demo_second_part_bot", None),
        (204, "qa", "ok", "/start → reply in 0.5 s", None),
        (207, "architect", "cmd", "catalog new tg-post-ranking --from bot/ranking.py --tests tests/test_ranking.py", None),
        (208, "qa", "ok", "/add @northroast → Watching @northroast", None),
        (211, "qa", "ok", "/remove @nope → That channel is not on your list.", None),
        (212, "architect", "out", "tg-post-ranking 0.1.0: rank(), Ranked, 5 tests", None),
        (214, "architect", "ok", "Opened brimos-catalog/tg-post-ranking pull request #1", None),
        (215, "qa", "ok", "Test digest: 3 channels, top 5 each, 1 post flagged 🔥", None),
        (218, "qa", "ok", "Links open the original posts", None),
        (220, "architect", "ok", "Catalog CI passed: 5 passed in 0.06s", None),
        (221, "qa", "ok", "Next digest: Monday 9:00", None),
        (225, "qa", "ok", "7 / 7 checks passed", None),
        (226, "brimos", "ok", "Catalog: published tg-post-ranking 0.1.0 — next products can reuse it", None),
        (228, "brimos", "ok", "Live: t.me/brimos_demo_second_part_bot", None),
    ]:
        add(Line(t, who, kind, text, None, f))
    return L


FEED = Feed(_build_script(), FILES, TASK_SPAN, SCAFFOLD_DONE, WORKER_OF, NAMES)
SCRIPT = FEED.script
repo_at, content, tree, live, worker_log_text = FEED.repo_at, FEED.content, FEED.tree, FEED.live, FEED.worker_log_text
