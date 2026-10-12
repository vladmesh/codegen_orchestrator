"""File contents of the scene-1 product repo (water-reminder-bot), as the agents write them.

Each entry in FILES is a list of versions; scene1_feed.py decides when each version lands.
Kept short on purpose: the dashboard shows them in a 1080p frame.
"""

PYPROJECT = '''[project]
name = "water-reminder-bot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "aiogram>=3.13",
    "apscheduler>=3.10,<4",
    "aiosqlite>=0.20",
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
      - run: pip install -e ".[test]"
      - run: ruff check .
      - run: pytest -q
'''

DOCKERFILE = '''FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY bot ./bot
VOLUME /app/data
CMD ["python", "-m", "bot.main"]
'''

GITIGNORE = '''__pycache__/
.venv/
data/
.pytest_cache/
'''

README = '''# Water Reminder Bot

Telegram bot that reminds you to drink water and keeps a daily streak.

- Reminders every 2 hours from 9:00 to 21:00, in your time zone
- Daily goal of 8 glasses, change it with /goal
- Weekly summary on Sundays
- /stats: today, current streak, best streak

Run: `TELEGRAM_BOT_TOKEN=... python -m bot.main`
'''

CONFIG = '''import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    token: str
    db_path: str = "data/bot.sqlite3"
    default_tz: str = "Europe/Belgrade"


def load() -> Config:
    return Config(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        db_path=os.environ.get("DB_PATH", "data/bot.sqlite3"),
    )
'''

MAIN_V1 = '''import asyncio
import logging

from aiogram import Bot, Dispatcher

from bot import config


async def main() -> None:
    cfg = config.load()
    bot = Bot(cfg.token)
    dp = Dispatcher()
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
'''

MAIN_V2 = MAIN_V1.replace(
    "from bot import config\n", "from bot import config, db\nfrom bot.handlers import reminders\nfrom bot.scheduler import ReminderScheduler\n"
).replace(
    '''    bot = Bot(cfg.token)
    dp = Dispatcher()
    await dp.start_polling(bot)''',
    '''    bot = Bot(cfg.token)
    store = await db.open(cfg.db_path)
    scheduler = ReminderScheduler(bot, store)

    dp = Dispatcher(store=store, scheduler=scheduler)
    dp.include_routers(reminders.router)

    await scheduler.start()
    await dp.start_polling(bot)''')

MAIN_V3 = MAIN_V2.replace("import reminders", "import goal, reminders").replace(
    "include_routers(reminders.router)", "include_routers(goal.router, reminders.router)")

MAIN_V4 = MAIN_V3.replace("import goal, reminders", "import commands, goal, reminders").replace(
    "include_routers(goal.router", "include_routers(commands.router, goal.router")

SCHEDULER = '''from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.handlers.reminders import log_glass_keyboard, reminder_text

FIRST_HOUR, LAST_HOUR, EVERY = 9, 21, 2


def reminder_trigger(tz: str) -> CronTrigger:
    hours = f"{FIRST_HOUR}-{LAST_HOUR}/{EVERY}"
    return CronTrigger(hour=hours, minute=0, timezone=ZoneInfo(tz))


class ReminderScheduler:
    def __init__(self, bot: Bot, store) -> None:
        self.bot, self.store = bot, store
        self.aps = AsyncIOScheduler()

    async def start(self) -> None:
        for user in await self.store.users():
            self.schedule(user.id, user.tz)
        self.aps.start()

    def schedule(self, user_id: int, tz: str) -> None:
        self.aps.add_job(
            self.remind, reminder_trigger(tz), args=[user_id],
            id=f"remind:{user_id}", replace_existing=True,
        )

    async def remind(self, user_id: int) -> None:
        today = await self.store.today(user_id)
        if today.glasses >= today.goal:
            return
        await self.bot.send_message(
            user_id, reminder_text(today.glasses, today.goal),
            reply_markup=log_glass_keyboard(),
        )
'''

# First take of the scheduler: the hour range stops at 20, so the 21:00 slot is lost.
SCHEDULER_V0 = SCHEDULER.replace("FIRST_HOUR, LAST_HOUR, EVERY = 9, 21, 2", "FIRST_HOUR, LAST_HOUR, EVERY = 9, 20, 2")

REMINDERS = '''from aiogram import Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

router = Router(name="reminders")


def log_glass_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="+1 glass", callback_data="glass:+1"),
        InlineKeyboardButton(text="Snooze 30 min", callback_data="snooze:30"),
    ]])


def reminder_text(glasses: int, goal: int) -> str:
    left = max(goal - glasses, 0)
    return f"Time for water: {glasses}/{goal} today, {left} to go"
'''

TEST_SCHEDULER = '''from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.handlers.reminders import reminder_text
from bot.scheduler import reminder_trigger


def fire_times(tz: str, day: str) -> list[str]:
    trigger = reminder_trigger(tz)
    t = datetime.fromisoformat(f"{day}T00:00").replace(tzinfo=ZoneInfo(tz))
    out = []
    while (t := trigger.get_next_fire_time(None, t)).date().isoformat() == day:
        out.append(t.strftime("%H:%M"))
        t = t.replace(second=1)
    return out


@pytest.mark.parametrize("hour", range(24))
def test_no_reminders_outside_9_to_21(hour):
    fired = {int(t[:2]) for t in fire_times("Europe/Belgrade", "2026-10-13")}
    assert (hour in fired) == (9 <= hour <= 21 and hour % 2 == 1)


def test_every_two_hours_from_9_to_21():
    assert fire_times("Europe/Belgrade", "2026-10-13") == [
        "09:00", "11:00", "13:00", "15:00", "17:00", "19:00", "21:00",
    ]


@pytest.mark.parametrize("tz", ["Asia/Tokyo", "America/New_York", "UTC"])
def test_first_reminder_is_9_local(tz):
    assert fire_times(tz, "2026-10-13")[0] == "09:00"


def test_reminder_text_counts_what_is_left():
    assert reminder_text(3, 8) == "Time for water: 3/8 today, 5 to go"
    assert reminder_text(9, 8).endswith("0 to go")
'''

DB_V0 = '''from dataclasses import dataclass
from datetime import datetime

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    tz TEXT NOT NULL,
    goal INTEGER NOT NULL DEFAULT 8
);
CREATE TABLE IF NOT EXISTS glasses (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


@dataclass
class User:
    id: int
    tz: str
    goal: int


@dataclass
class Today:
    glasses: int
    goal: int

    @property
    def reached(self) -> bool:
        return self.glasses >= self.goal


class Store:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    async def ensure_user(self, user_id: int, tz: str) -> User:
        await self.conn.execute(
            "INSERT OR IGNORE INTO users (id, tz) VALUES (?, ?)", (user_id, tz)
        )
        await self.conn.commit()
        return await self.user(user_id)

    async def user(self, user_id: int) -> User:
        cur = await self.conn.execute("SELECT id, tz, goal FROM users WHERE id = ?", (user_id,))
        return User(*await cur.fetchone())

    async def set_goal(self, user_id: int, goal: int) -> None:
        await self.conn.execute("UPDATE users SET goal = ? WHERE id = ?", (goal, user_id))
        await self.conn.commit()

    async def add_glass(self, user_id: int, at: datetime) -> int:
        await self.conn.execute(
            "INSERT INTO glasses (user_id, day, at) VALUES (?, ?, ?)",
            (user_id, at.date().isoformat(), at.isoformat()),
        )
        await self.conn.commit()
        return (await self.today(user_id, at)).glasses

    async def today(self, user_id: int, now: datetime) -> Today:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM glasses WHERE user_id = ? AND day = ?",
            (user_id, now.date().isoformat()),
        )
        (count,) = await cur.fetchone()
        return Today(count, (await self.user(user_id)).goal)


async def open(path) -> Store:
    conn = await aiosqlite.connect(path)
    await conn.executescript(SCHEMA)
    return Store(conn)
'''

DB_V1 = DB_V0.replace(
    '''    async def set_goal(self, user_id: int, goal: int) -> None:
        await self.conn''',
    '''    async def set_goal(self, user_id: int, goal: int) -> None:
        if not 1 <= goal <= 30:
            raise ValueError("goal must be between 1 and 30 glasses")
        await self.conn''')

DB_V2 = DB_V1.replace(
    '''    at TEXT NOT NULL
);
"""''',
    '''    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS streaks (
    user_id INTEGER PRIMARY KEY,
    current INTEGER NOT NULL DEFAULT 0,
    best INTEGER NOT NULL DEFAULT 0,
    last_day TEXT
);
"""''').replace(
    '''class Store:''',
    '''@dataclass
class Streak:
    current: int = 0
    best: int = 0
    last_day: date | None = None


class Store:''').replace(
    "from datetime import datetime", "from datetime import date, datetime").replace(
    '''

async def open(path)''',
    '''
    async def streak(self, user_id: int) -> Streak:
        cur = await self.conn.execute(
            "SELECT current, best, last_day FROM streaks WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return Streak()
        return Streak(row[0], row[1], date.fromisoformat(row[2]) if row[2] else None)

    async def save_streak(self, user_id: int, s: Streak) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO streaks VALUES (?, ?, ?, ?)",
            (user_id, s.current, s.best, s.last_day and s.last_day.isoformat()),
        )
        await self.conn.commit()


async def open(path)''')

DB_V3 = DB_V2.replace(
    '''

async def open(path)''',
    '''
    async def last_7_days(self, user_id: int) -> list[int]:
        cur = await self.conn.execute(
            "SELECT day, COUNT(*) FROM glasses WHERE user_id = ? "
            "AND day >= date(\'now\', \'-6 days\') GROUP BY day", (user_id,),
        )
        return [count for _, count in await cur.fetchall()]


async def open(path)''')

GOAL = '''from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

router = Router(name="goal")


@router.message(Command("goal"))
async def set_goal(message: Message, command: CommandObject, store) -> None:
    if not command.args or not command.args.isdigit():
        await message.answer("Usage: /goal 8")
        return
    try:
        await store.set_goal(message.from_user.id, int(command.args))
    except ValueError as e:
        await message.answer(str(e).capitalize())
        return
    await message.answer(f"Daily goal set to {command.args} glasses")
'''

CONFTEST = '''from datetime import datetime, timezone

import pytest

from bot import db


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture
async def store(tmp_path):
    s = await db.open(tmp_path / "test.sqlite3")
    yield s
    await s.conn.close()


@pytest.fixture
async def user(store):
    return await store.ensure_user(1001, tz="UTC")
'''

CONFTEST_V2 = CONFTEST + '''

class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[str] = []

    def schedule(self, user_id: int, tz: str) -> None:
        self.jobs.append(f"remind:{user_id}")


@pytest.fixture
def fake_scheduler():
    return FakeScheduler()
'''

TEST_GOAL = '''import pytest

from tests.conftest import utc

NOON = utc(2026, 10, 13, 12, 0)


async def test_glasses_add_up_within_a_day(store, user):
    await store.add_glass(user.id, NOON)
    assert await store.add_glass(user.id, NOON) == 2


async def test_goal_defaults_to_eight(store, user):
    assert (await store.today(user.id, NOON)).goal == 8


async def test_goal_can_be_changed(store, user):
    await store.set_goal(user.id, 6)
    assert (await store.today(user.id, NOON)).goal == 6


@pytest.mark.parametrize("goal", [0, 31])
async def test_goal_rejects_out_of_range(store, user, goal):
    with pytest.raises(ValueError):
        await store.set_goal(user.id, goal)
'''

STREAK_V1 = '''from datetime import datetime, timedelta


async def close_day(store, user, now: datetime) -> int:
    """Called after every logged glass. Returns the current streak."""
    day = now.date()
    s = await store.streak(user.id)
    today = await store.today(user.id, now)
    if today.reached and s.last_day != day:
        s.current = s.current + 1 if s.last_day == day - timedelta(days=1) else 1
        s.best = max(s.best, s.current)
        s.last_day = day
        await store.save_streak(user.id, s)
    return s.current


async def current_streak(store, user, now: datetime) -> int:
    s = await store.streak(user.id)
    day = now.date()
    if s.last_day is None or s.last_day < day - timedelta(days=1):
        return 0
    return s.current
'''

STREAK_V2 = STREAK_V1.replace(
    '''from datetime import datetime, timedelta
''',
    '''from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def local_day(user, now: datetime) -> date:
    """The streak day ends at the user's midnight, not at UTC midnight."""
    return now.astimezone(ZoneInfo(user.tz)).date()
''').replace("    day = now.date()\n", "    day = local_day(user, now)\n")

TEST_STREAK = '''from bot.streak import close_day, current_streak
from tests.conftest import utc


async def reach_goal(store, user, at):
    for _ in range(8):
        await store.add_glass(user.id, at)
    return await close_day(store, user, at)


async def test_streak_grows_on_consecutive_days(store, user):
    assert await reach_goal(store, user, utc(2026, 10, 11, 18, 0)) == 1
    assert await reach_goal(store, user, utc(2026, 10, 12, 18, 0)) == 2


async def test_missed_day_resets_streak(store, user):
    await reach_goal(store, user, utc(2026, 10, 10, 18, 0))
    assert await current_streak(store, user, now=utc(2026, 10, 12, 9, 0)) == 0


async def test_best_streak_is_kept(store, user):
    for day in (8, 9, 10, 12):
        await reach_goal(store, user, utc(2026, 10, day, 18, 0))
    assert (await store.streak(user.id)).best == 3
'''

SUMMARY = '''from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger


def summary_text(days: list[int], goal: int, streak: int) -> str:
    hit = sum(1 for glasses in days if glasses >= goal)
    return (
        f"Your week: {sum(days)} glasses, goal reached {hit} of 7 days.\\n"
        f"Current streak: {streak} days."
    )


async def send_summary(bot, store, user_id: int) -> None:
    user = await store.user(user_id)
    week = await store.last_7_days(user_id)
    streak = await store.streak(user_id)
    await bot.send_message(user_id, summary_text(week, user.goal, streak.current))


def schedule_summary(aps, bot, store, user) -> None:
    aps.add_job(
        send_summary,
        CronTrigger(day_of_week="sun", hour=20, timezone=ZoneInfo(user.tz)),
        args=[bot, store, user.id], id=f"summary:{user.id}", replace_existing=True,
    )
'''

TEST_SUMMARY = '''from bot.summary import summary_text


def test_counts_days_with_goal_reached():
    text = summary_text([8, 8, 5, 9, 8, 2, 8], goal=8, streak=1)
    assert "goal reached 5 of 7 days" in text


def test_total_glasses():
    assert "46 glasses" in summary_text([8, 8, 5, 9, 8, 0, 8], goal=8, streak=0)


def test_streak_line():
    assert summary_text([8] * 7, goal=8, streak=12).endswith("Current streak: 12 days.")
'''

COMMANDS = '''from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from bot.handlers.reminders import log_glass_keyboard
from bot.streak import close_day, current_streak

router = Router(name="commands")


@router.message(CommandStart())
async def start(message: Message, store, scheduler) -> None:
    user = await store.ensure_user(message.from_user.id, tz="Europe/Belgrade")
    scheduler.schedule(user.id, user.tz)
    await message.answer(
        "Hi! I'll remind you to drink water every 2 hours from 9:00 to 21:00.\\n"
        f"Daily goal: {user.goal} glasses. Change it with /goal.",
        reply_markup=log_glass_keyboard(),
    )


@router.callback_query(F.data == "glass:+1")
async def log_glass(call: CallbackQuery, store) -> None:
    now = datetime.now(timezone.utc)
    user = await store.user(call.from_user.id)
    count = await store.add_glass(user.id, now)
    streak = await close_day(store, user, now)
    await call.answer(f"{count}/{user.goal} today, streak {streak}")


@router.message(Command("stats"))
async def stats(message: Message, store) -> None:
    now = datetime.now(timezone.utc)
    user = await store.user(message.from_user.id)
    today = await store.today(user.id, now)
    best = (await store.streak(user.id)).best
    streak = await current_streak(store, user, now)
    await message.answer(
        f"Today: {today.glasses}/{today.goal}\\nStreak: {streak} days\\nBest: {best} days"
    )
'''

FAKES = '''class FakeMessage:
    def __init__(self, user_id: int, text: str = "") -> None:
        self.from_user = type("User", (), {"id": user_id})()
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **_) -> None:
        self.answers.append(text)
'''

TEST_COMMANDS = '''from bot.handlers import commands
from tests.fakes import FakeMessage


async def test_stats_on_a_fresh_account(store, user):
    msg = FakeMessage(user.id, "/stats")
    await commands.stats(msg, store)
    assert msg.answers[-1].splitlines() == ["Today: 0/8", "Streak: 0 days", "Best: 0 days"]


async def test_start_schedules_reminders(store, fake_scheduler):
    msg = FakeMessage(2002, "/start")
    await commands.start(msg, store, fake_scheduler)
    assert fake_scheduler.jobs == ["remind:2002"]
    assert "every 2 hours from 9:00 to 21:00" in msg.answers[0]
'''

ACCEPTANCE = '''"""Checks written by the architect from the story's acceptance criteria."""
from bot.handlers.reminders import reminder_text
from bot.scheduler import reminder_trigger
from bot.streak import close_day, current_streak
from bot.summary import summary_text
from tests.conftest import utc


async def make_user(store, tz, goal=8):
    user = await store.ensure_user(4242, tz=tz)
    await store.set_goal(user.id, goal)
    return user


async def log_glasses(store, user, n, at):
    for _ in range(n):
        await store.add_glass(user.id, at)
    await close_day(store, user, at)


def test_reminders_window():
    trigger = reminder_trigger("Europe/Belgrade")
    assert str(trigger.fields[5]) == "9-21/2"


async def test_goal_is_adjustable(store):
    user = await make_user(store, "UTC", goal=6)
    assert (await store.today(user.id, utc(2026, 10, 13, 9, 0))).goal == 6


async def test_streak_grows_when_goal_reached(store):
    user = await make_user(store, "Europe/Belgrade")
    await log_glasses(store, user, 8, at=utc(2026, 10, 12, 15, 0))
    await log_glasses(store, user, 8, at=utc(2026, 10, 13, 15, 0))
    assert await current_streak(store, user, now=utc(2026, 10, 13, 16, 0)) == 2


async def test_streak_resets_at_midnight(store):
    user = await make_user(store, "Asia/Tokyo")
    await log_glasses(store, user, 8, at=utc(2026, 10, 11, 13, 0))  # Oct 11, 22:00 Tokyo
    # Oct 12 is missed; now it is Oct 13, 08:00 in Tokyo
    assert await current_streak(store, user, now=utc(2026, 10, 12, 23, 0)) == 0, \\
        "streak should reset after a missed local day"


def test_weekly_summary_text():
    assert "goal reached 7 of 7 days" in summary_text([8] * 7, goal=8, streak=7)


def test_reminder_text():
    assert reminder_text(0, 8) == "Time for water: 0/8 today, 8 to go"
'''

FILES: dict[str, list[str]] = {
    "README.md": [README],
    "pyproject.toml": [PYPROJECT],
    "Dockerfile": [DOCKERFILE],
    ".gitignore": [GITIGNORE],
    ".github/workflows/ci.yml": [CI_YML],
    "bot/__init__.py": [""],
    "bot/config.py": [CONFIG],
    "bot/main.py": [MAIN_V1, MAIN_V2, MAIN_V3, MAIN_V4],
    "bot/handlers/__init__.py": [""],
    "bot/scheduler.py": [SCHEDULER_V0, SCHEDULER],
    "bot/handlers/reminders.py": [REMINDERS],
    "tests/test_scheduler.py": [TEST_SCHEDULER],
    "bot/db.py": [DB_V0, DB_V1, DB_V2, DB_V3],
    "bot/handlers/goal.py": [GOAL],
    "tests/conftest.py": [CONFTEST, CONFTEST_V2],
    "tests/test_goal.py": [TEST_GOAL],
    "bot/streak.py": [STREAK_V1, STREAK_V2],
    "tests/test_streak.py": [TEST_STREAK],
    "bot/summary.py": [SUMMARY],
    "tests/test_summary.py": [TEST_SUMMARY],
    "bot/handlers/commands.py": [COMMANDS],
    "tests/fakes.py": [FAKES],
    "tests/test_commands.py": [TEST_COMMANDS],
    "tests/test_acceptance.py": [ACCEPTANCE],
}


def failing_line() -> int:
    """Line of the assert that CI run 1 trips on, so the CI log points at the real line."""
    for n, line in enumerate(ACCEPTANCE.splitlines(), 1):
        if "now=utc(2026, 10, 12, 23, 0)) == 0" in line:
            return n
    raise AssertionError("acceptance test changed")
