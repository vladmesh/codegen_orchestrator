"""The generated /start access probe, run on a virtual clock against a fake Telethon."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import sys
import time
from types import ModuleType, SimpleNamespace

from shared.contracts.bot_access import QA_TEST_TELEGRAM_ID
from shared.contracts.dto.run_result import QABlockerCategory
from shared.telegram_access_probe import (
    PROBE_PASSED_MARKER,
    build_access_probe_script,
    classify_access_probe,
)


class _Bot:
    """A bot whose answers after /start (message 10) are ``chat(now)``, oldest first."""

    def __init__(self, chat, *, account_id=QA_TEST_TELEGRAM_ID):
        self.now = 0
        self.chat = chat
        self.account_id = account_id
        self.reads: list[float] = []
        self.sent: list[str] = []
        self.disconnected = False

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds

    def client(self, *_args):
        bot = self

        class Client:
            def start(self):
                return None

            def get_me(self):
                return SimpleNamespace(id=bot.account_id)

            def get_entity(self, username):
                return username

            def send_message(self, _entity, text):
                bot.sent.append(text)
                return SimpleNamespace(id=10)

            def get_messages(self, _entity, *, min_id, limit):
                assert (min_id, limit) == (10, 5)
                bot.reads.append(bot.now)
                return list(reversed(bot.chat(bot.now)))

            def disconnect(self):
                bot.disconnected = True

        return Client()


def _answer(identifier, text):
    return SimpleNamespace(id=identifier, out=False, raw_text=text, message=text)


def _run(monkeypatch, bot: _Bot) -> tuple[int, str]:
    monkeypatch.setattr(time, "monotonic", lambda: bot.now)
    monkeypatch.setattr(time, "sleep", bot.sleep)
    for name in ("TELETHON_SESSION", "TELETHON_API_HASH"):
        monkeypatch.setenv(name, "fixture")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    sync = ModuleType("telethon.sync")
    sync.TelegramClient = bot.client
    sessions = ModuleType("telethon.sessions")
    sessions.StringSession = lambda value: value
    monkeypatch.setitem(sys.modules, "telethon", ModuleType("telethon"))
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    stdout = StringIO()
    exit_status = 0
    with redirect_stdout(stdout):
        try:
            exec(build_access_probe_script("fortune_bot"), {})  # noqa: S102 - generated probe
        except SystemExit as exc:
            exit_status = exc.code
    return exit_status, stdout.getvalue()


def _verdict(exit_status, stdout):
    return classify_access_probe(
        exit_status=exit_status, stdout=stdout, stderr="", bot_username="fortune_bot"
    )


def test_a_stable_welcome_passes_once_the_quiet_window_ends(monkeypatch):
    bot = _Bot(lambda _now: [_answer(11, "Welcome!")])

    exit_status, stdout = _run(monkeypatch, bot)

    assert (exit_status, stdout.strip()) == (0, PROBE_PASSED_MARKER)
    assert _verdict(exit_status, stdout) is None
    assert bot.sent == ["/start"]
    assert bot.reads == [0, 1, 2, 3]
    assert bot.disconnected


def test_successive_answers_reset_the_quiet_window(monkeypatch):
    bot = _Bot(lambda now: [_answer(11, "Welcome!"), _answer(12, "Menu")][: 1 + (now >= 2)])

    exit_status, _stdout = _run(monkeypatch, bot)

    assert exit_status == 0
    assert bot.reads == [0, 1, 2, 3, 4, 5]


def test_silence_runs_to_the_deadline_and_is_not_a_denial(monkeypatch):
    bot = _Bot(lambda _now: [])

    exit_status, stdout = _run(monkeypatch, bot)

    assert _verdict(exit_status, stdout) is None
    assert bot.reads == list(range(11))
    assert bot.disconnected


def test_a_denial_after_a_first_answer_blocks_before_the_answers_settle(monkeypatch):
    bot = _Bot(
        lambda now: [_answer(11, "Hello"), _answer(12, "Access denied for you")][: 1 + (now >= 2)]
    )

    exit_status, stdout = _run(monkeypatch, bot)

    blocker = _verdict(exit_status, stdout)
    assert exit_status == 2
    assert blocker.category == QABlockerCategory.TELEGRAM_ACCESS_DENIED
    assert blocker.received == "Access denied for you"
    assert bot.reads == [0, 1, 2]
    assert bot.disconnected


def test_another_account_is_refused_before_anything_is_sent(monkeypatch):
    bot = _Bot(lambda _now: [], account_id=QA_TEST_TELEGRAM_ID + 1)

    exit_status, stdout = _run(monkeypatch, bot)

    blocker = _verdict(exit_status, stdout)
    assert exit_status == 3
    assert blocker.category == QABlockerCategory.UNKNOWN
    assert blocker.received == f"expected={QA_TEST_TELEGRAM_ID};actual={QA_TEST_TELEGRAM_ID + 1}"
    assert bot.sent == []
    assert bot.disconnected
