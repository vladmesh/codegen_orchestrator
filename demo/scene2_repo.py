"""File contents of the scene-2 product repo (competitor-digest-bot), as the agents write them.

Each entry in FILES is a list of versions; scene2_feed.py decides when each version lands.
The channel reading is not here: it comes from the catalog package tg-channels (installed by the
scaffolder) and the platform service tg-reader. The repo holds only the new logic.
"""

PYPROJECT = '''[project]
name = "competitor-digest-bot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "aiogram>=3.13",
    "apscheduler>=3.10,<4",
    "aiosqlite>=0.20",
    "tg-channels==0.1.2",  # Brimos catalog: posts and views via tg-reader
]

[project.optional-dependencies]
test = ["pytest>=8.3", "pytest-asyncio>=0.24", "ruff>=0.6"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
'''

CI_YML = '''name: ci
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[test]" --extra-index-url https://catalog.brimos.dev/simple
      - run: ruff check .
      - run: pytest -q
'''

DOCKERFILE = '''FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir . --extra-index-url https://catalog.brimos.dev/simple
COPY bot ./bot
VOLUME /app/data
CMD ["python", "-m", "bot.main"]
'''

GITIGNORE = '''__pycache__/
.venv/
data/
.pytest_cache/
'''

README = '''# Competitor Digest Bot

Every Monday at 9:00 sends a digest of what competitors posted in their Telegram channels.

- Top 5 posts per channel by views for the past 7 days
- Posts that took off (3x the channel's weekly median) are flagged 🔥
- /add @channel and /remove @channel change the list

Channel posts come from the Brimos catalog package `tg-channels`
through the platform service `tg-reader`: no Telegram account needed.
'''

CONFIG = '''import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bot_token: str
    tg_reader_url: str
    db_path: str = "data/digest.db"


def load() -> Config:
    return Config(
        bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        # platform service, injected by Brimos at deploy
        tg_reader_url=os.environ["TG_READER_URL"],
    )
'''

SOURCES = '''"""Channel posts from the catalog package tg-channels (platform service tg-reader)."""
from datetime import datetime, timedelta

from tg_channels import ChannelReader, Post

__all__ = ["Post", "week_of_posts"]


async def week_of_posts(reader: ChannelReader, channel: str, now: datetime) -> list[Post]:
    """Every post of the past 7 days, with its views and link."""
    return await reader.posts(channel, since=now - timedelta(days=7))
'''

MAIN_0 = '''import asyncio

from aiogram import Bot, Dispatcher
from tg_channels import ChannelReader

from bot import config


async def main() -> None:
    cfg = config.load()
    bot = Bot(cfg.bot_token)
    reader = ChannelReader(cfg.tg_reader_url)
    dp = Dispatcher(reader=reader)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
'''

MAIN_1 = '''import asyncio

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from tg_channels import ChannelReader

from bot import config, digest, store
from bot.handlers import channels


async def main() -> None:
    cfg = config.load()
    bot = Bot(cfg.bot_token)
    reader = ChannelReader(cfg.tg_reader_url)
    db = await store.open(cfg.db_path)
    aps = AsyncIOScheduler()
    dp = Dispatcher(reader=reader, store=db)
    dp.include_router(channels.router)
    for user in await db.users():
        digest.schedule(aps, bot, reader, db, user)
    aps.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
'''

RANKING_0 = '''"""Rank a channel's posts by views and flag the ones that took off."""
from dataclasses import dataclass
from statistics import median

from tg_channels import Post

TOOK_OFF = 3.0  # x the channel's weekly median views


@dataclass(frozen=True)
class Ranked:
    post: Post
    took_off: bool


def rank(posts: list[Post], top: int = 5) -> list[Ranked]:
    if not posts:
        return []
    mid = median(p.views for p in posts)
    best = sorted(posts, key=lambda p: p.views, reverse=True)[:top]
    return [Ranked(p, p.views > TOOK_OFF * mid) for p in best]
'''

RANKING_1 = '''"""Rank a channel's posts by views and flag the ones that took off."""
from dataclasses import dataclass
from statistics import median

from tg_channels import Post

TOOK_OFF = 3.0  # x the channel's weekly median views


@dataclass(frozen=True)
class Ranked:
    post: Post
    took_off: bool


def rank(posts: list[Post], top: int = 5) -> list[Ranked]:
    if not posts:
        return []
    mid = median(p.views for p in posts)
    # newer post first on equal views, so the order is stable week to week
    best = sorted(posts, key=lambda p: (p.views, p.date), reverse=True)[:top]
    return [Ranked(p, mid > 0 and p.views >= TOOK_OFF * mid) for p in best]
'''

TEST_RANKING = '''from datetime import datetime

from tg_channels import Post

from bot.ranking import rank


def post(views: int, day: int = 1) -> Post:
    return Post(channel="@northroast", id=day * 100 + views % 97, date=datetime(2026, 10, day, 9),
                text="New single origin: Ethiopia Guji", views=views, url="https://t.me/northroast/1")


def test_top_five_by_views():
    posts = [post(v) for v in (120, 900, 340, 80, 410, 260, 150)]
    assert [r.post.views for r in rank(posts)] == [900, 410, 340, 260, 150]


def test_flags_three_times_the_median():
    posts = [post(v) for v in (100, 110, 90, 105, 330)]
    flags = {r.post.views: r.took_off for r in rank(posts)}
    assert flags[330] is True and flags[110] is False


def test_exactly_three_times_is_flagged():
    assert rank([post(100), post(100), post(300)])[0].took_off


def test_empty_week():
    assert rank([]) == []


def test_all_zero_views_flags_nothing():
    assert not any(r.took_off for r in rank([post(0), post(0)]))
'''

STORE = '''"""Users and their watched channels (sqlite)."""
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, tz TEXT NOT NULL DEFAULT 'UTC');
CREATE TABLE IF NOT EXISTS channels (user_id INTEGER, handle TEXT, PRIMARY KEY (user_id, handle));
"""


class Store:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def users(self) -> list[int]:
        rows = await self.db.execute_fetchall("SELECT id FROM users")
        return [r[0] for r in rows]

    async def channels(self, user_id: int) -> list[str]:
        rows = await self.db.execute_fetchall(
            "SELECT handle FROM channels WHERE user_id = ? ORDER BY handle", (user_id,))
        return [r[0] for r in rows]

    async def add(self, user_id: int, handle: str) -> None:
        await self.db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
        await self.db.execute("INSERT OR IGNORE INTO channels VALUES (?, ?)", (user_id, handle))
        await self.db.commit()

    async def remove(self, user_id: int, handle: str) -> bool:
        cur = await self.db.execute("DELETE FROM channels WHERE user_id = ? AND handle = ?", (user_id, handle))
        await self.db.commit()
        return cur.rowcount > 0


async def open(path: str) -> Store:
    db = await aiosqlite.connect(path)
    await db.executescript(SCHEMA)
    return Store(db)
'''

DIGEST = '''"""The Monday digest: top posts of every watched channel for the past 7 days."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from bot.ranking import rank
from bot.sources import week_of_posts


def excerpt(text: str, width: int = 70) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= width else line[: width - 1].rstrip() + "…"


def render(by_channel: dict) -> str:
    parts = ["<b>Your competitors this week</b>"]
    for channel, ranked in by_channel.items():
        parts.append(f"\\n<b>{channel}</b>")
        if not ranked:
            parts.append("no posts this week")
        for i, r in enumerate(ranked, 1):
            fire = " 🔥" if r.took_off else ""
            parts.append(f'{i}. {excerpt(r.post.text)} · {r.post.views:,} views{fire} <a href="{r.post.url}">→</a>')
    return "\\n".join(parts)


async def build(reader, store, user_id: int, now: datetime) -> str:
    by_channel = {}
    for channel in await store.channels(user_id):
        by_channel[channel] = rank(await week_of_posts(reader, channel, now))
    return render(by_channel)


def schedule(aps, bot, reader, store, user_id: int, tz: str = "UTC") -> None:
    async def send() -> None:
        text = await build(reader, store, user_id, datetime.now(timezone.utc))
        await bot.send_message(user_id, text, parse_mode="HTML", disable_web_page_preview=True)

    aps.add_job(send, CronTrigger(day_of_week="mon", hour=9, timezone=ZoneInfo(tz)), id=f"digest-{user_id}")
'''

CHANNELS = '''from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

router = Router()


def handle(arg: str | None) -> str | None:
    if not arg:
        return None
    h = arg.strip().split()[0].removeprefix("https://t.me/")
    return h if h.startswith("@") else f"@{h}"


@router.message(Command("add"))
async def add(message: Message, command: CommandObject, store) -> None:
    h = handle(command.args)
    if not h:
        await message.answer("Send it like this: /add @channel")
        return
    await store.add(message.chat.id, h)
    await message.answer(f"Watching {h}. Next digest on Monday at 9:00.")


@router.message(Command("remove"))
async def remove(message: Message, command: CommandObject, store) -> None:
    h = handle(command.args)
    removed = h is not None and await store.remove(message.chat.id, h)
    await message.answer(f"Stopped watching {h}." if removed else "That channel is not on your list.")
'''

HANDLERS_INIT = ""

TEST_DIGEST = '''from datetime import datetime

from tg_channels import Post

from bot.digest import excerpt, render
from bot.handlers.channels import handle
from bot.ranking import Ranked


def test_excerpt_first_line_cut_to_width():
    assert excerpt("Short news\\nsecond line") == "Short news"
    assert excerpt("x" * 100).endswith("…") and len(excerpt("x" * 100)) == 70


def test_render_marks_posts_that_took_off():
    p = Post(channel="@kaffa_lab", id=7, date=datetime(2026, 10, 12), text="Cold brew is back",
             views=4200, url="https://t.me/kaffa_lab/7")
    text = render({"@kaffa_lab": [Ranked(p, True)], "@beanbox_daily": []})
    assert "1. Cold brew is back · 4,200 views 🔥" in text
    assert "@beanbox_daily</b>\\nno posts this week" in text


def test_channel_handles():
    assert handle("northroast") == "@northroast"
    assert handle("https://t.me/kaffa_lab") == "@kaffa_lab"
    assert handle(None) is None
'''

ACCEPTANCE = '''"""Checks written by the architect from the story's acceptance criteria."""
from datetime import datetime

from tg_channels.testing import FakeReader, week

from bot import digest
from bot.ranking import rank


async def test_digest_covers_past_7_days_of_every_channel(store):
    reader = FakeReader({"@northroast": week(posts=9), "@kaffa_lab": week(posts=4, old=3)})
    for ch in ("@northroast", "@kaffa_lab"):
        await store.add(1, ch)
    text = await digest.build(reader, store, 1, datetime(2026, 10, 19, 9))
    assert text.count(" views") == 5 + 4


def test_top_5_and_flag_at_3x_median():
    ranked = rank(week(posts=12, viral=1))
    assert len(ranked) == 5 and ranked[0].took_off


def test_monday_9_in_user_tz(fake_aps):
    digest.schedule(fake_aps, None, None, None, 1, tz="Europe/Belgrade")
    assert fake_aps.next_fire("digest-1", after="2026-10-15T12:00") == "2026-10-19T09:00+02:00"
'''

CONFTEST = '''import pytest

from bot import store as store_mod


@pytest.fixture
async def store(tmp_path):
    return await store_mod.open(str(tmp_path / "t.db"))
'''

FILES: dict[str, list[str]] = {
    "pyproject.toml": [PYPROJECT],
    "README.md": [README],
    ".gitignore": [GITIGNORE],
    "Dockerfile": [DOCKERFILE],
    ".github/workflows/ci.yml": [CI_YML],
    "bot/__init__.py": [""],
    "bot/config.py": [CONFIG],
    "bot/sources.py": [SOURCES],
    "bot/main.py": [MAIN_0, MAIN_1],
    "bot/ranking.py": [RANKING_0, RANKING_1],
    "tests/test_ranking.py": [TEST_RANKING],
    "bot/store.py": [STORE],
    "bot/digest.py": [DIGEST],
    "bot/handlers/__init__.py": [HANDLERS_INIT],
    "bot/handlers/channels.py": [CHANNELS],
    "tests/conftest.py": [CONFTEST],
    "tests/test_digest.py": [TEST_DIGEST],
    "tests/test_acceptance.py": [ACCEPTANCE],
}
