"""Telegram probe wire-format regressions."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import time
from types import ModuleType

from shared.telegram_bot_probe import (
    PROBE_RESULT_MARKER,
    build_bot_callback_script,
    build_bot_message_script,
    parse_bot_probe_result,
)


def test_reply_evidence_keeps_media_caption_and_keyboard_data_separate():
    payload = {
        "action": "message",
        "attempted": "send /forecast to @weather_bot",
        "sent": "/forecast",
        "delivered": True,
        "replies": [
            {
                "id": 42,
                "text": None,
                "caption": "Today: sunny",
                "media_type": "MessageMediaPhoto",
                "reply_markup": {
                    "type": "ReplyInlineMarkup",
                    "buttons": [
                        {
                            "row": 0,
                            "column": 0,
                            "text": "Details",
                            "type": "KeyboardButtonCallback",
                            "callback_data": "ZGV0YWlscw==",
                        },
                    ],
                },
            },
            {
                "id": 43,
                "text": "Choose a city",
                "caption": None,
                "media_type": None,
                "reply_markup": {
                    "type": "ReplyKeyboardMarkup",
                    "buttons": [
                        {
                            "row": 0,
                            "column": 0,
                            "text": "Share contact",
                            "type": "KeyboardButtonRequestPhone",
                            "callback_data": None,
                        }
                    ],
                },
            },
        ],
        "callback": None,
        "error": None,
    }

    evidence = parse_bot_probe_result(PROBE_RESULT_MARKER + json.dumps(payload))

    reply, reply_keyboard = evidence["replies"]
    assert reply["text"] is None
    assert reply["caption"] == "Today: sunny"
    assert reply["media_type"] == "MessageMediaPhoto"
    assert reply["reply_markup"]["buttons"][0]["callback_data"] == "ZGV0YWlscw=="
    assert reply_keyboard["reply_markup"]["type"] == "ReplyKeyboardMarkup"
    assert reply_keyboard["reply_markup"]["buttons"][0]["type"] == "KeyboardButtonRequestPhone"


def test_pre_delivery_error_is_explicit_in_the_probe_result():
    payload = {
        "action": "message",
        "attempted": "send an empty message to @weather_bot",
        "sent": "",
        "delivered": False,
        "replies": [],
        "callback": None,
        "error": "ValueError: The message cannot be empty",
    }

    evidence = parse_bot_probe_result(PROBE_RESULT_MARKER + json.dumps(payload))

    assert evidence["delivered"] is False
    assert evidence["error"].startswith("ValueError:")


def test_empty_telethon_message_error_is_emitted_as_undelivered_evidence(monkeypatch):
    class FakeClient:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def get_entity(self, _username):
            return "weather-bot"

        def send_message(self, _bot, _message):
            raise ValueError("The message cannot be empty")

        def disconnect(self):
            return None

    telethon = ModuleType("telethon")
    sync = ModuleType("telethon.sync")
    sync.TelegramClient = FakeClient
    sessions = ModuleType("telethon.sessions")
    sessions.StringSession = lambda value: value
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(build_bot_message_script("weather_bot", ""), {})  # noqa: S102 - generated probe source

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["delivered"] is False
    assert evidence["error"] == "ValueError: The message cannot be empty"


def test_generated_message_serializer_keeps_media_markup_and_link_preview_text(monkeypatch):
    """Exercise the serializer the Telethon child actually runs, not its parsed output."""

    class MessageMediaPhoto:
        pass

    class MessageMediaWebPage:
        pass

    class KeyboardButtonCallback:
        def __init__(self, text, data):
            self.text = text
            self.data = data

    class KeyboardButtonRequestPhone:
        def __init__(self, text):
            self.text = text
            self.data = None

    class Row:
        def __init__(self, *buttons):
            self.buttons = buttons

    class ReplyInlineMarkup:
        def __init__(self, *rows):
            self.rows = rows

    class ReplyKeyboardMarkup:
        def __init__(self, *rows):
            self.rows = rows

    class FakeClient:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def get_entity(self, _username):
            return "weather-bot"

        def send_message(self, _bot, _message):
            return type("Sent", (), {"id": 10})()

        def get_messages(self, _bot, *, min_id, limit):
            assert (min_id, limit) == (10, 10)
            # Telethon returns newest first; the probe restores chronological order.
            return [
                type(
                    "Message",
                    (),
                    {
                        "id": 12,
                        "out": False,
                        "raw_text": "See https://example.com",
                        "message": "See https://example.com",
                        "media": MessageMediaWebPage(),
                        "reply_markup": ReplyKeyboardMarkup(
                            Row(KeyboardButtonRequestPhone("Share contact"))
                        ),
                    },
                )(),
                type(
                    "Message",
                    (),
                    {
                        "id": 11,
                        "out": False,
                        "raw_text": "",
                        "message": "",
                        "media": MessageMediaPhoto(),
                        "reply_markup": ReplyInlineMarkup(
                            Row(KeyboardButtonCallback("Details", b"details"))
                        ),
                    },
                )(),
            ]

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_message_script("weather_bot", "/forecast", wait_seconds=1),
            {"__name__": "telegram_probe_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())
    media_reply, link_preview_reply = evidence["replies"]

    assert media_reply == {
        "id": 11,
        "text": None,
        "caption": None,
        "media_type": "MessageMediaPhoto",
        "reply_markup": {
            "type": "ReplyInlineMarkup",
            "buttons": [
                {
                    "row": 0,
                    "column": 0,
                    "text": "Details",
                    "type": "KeyboardButtonCallback",
                    "callback_data": "ZGV0YWlscw==",
                }
            ],
        },
    }
    assert link_preview_reply["text"] == "See https://example.com"
    assert link_preview_reply["caption"] is None
    assert link_preview_reply["media_type"] == "MessageMediaWebPage"
    assert link_preview_reply["reply_markup"]["type"] == "ReplyKeyboardMarkup"
    assert link_preview_reply["reply_markup"]["buttons"][0]["type"] == "KeyboardButtonRequestPhone"


def test_generated_callback_uses_a_pre_press_baseline_and_records_an_edit(monkeypatch):
    """Only messages after the press are callback replies; edits are evidence too."""

    class KeyboardButtonCallback:
        def __init__(self, text, data):
            self.text = text
            self.data = data

    class Row:
        def __init__(self, *buttons):
            self.buttons = buttons

    class ReplyInlineMarkup:
        def __init__(self, *rows):
            self.rows = rows

    class Request:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    original = type(
        "Message",
        (),
        {
            "id": 7,
            "out": False,
            "raw_text": "Choose",
            "message": "Choose",
            "media": None,
            "reply_markup": ReplyInlineMarkup(Row(KeyboardButtonCallback("Details", b"details"))),
        },
    )()
    edited = type(
        "Message",
        (),
        {
            "id": 7,
            "out": False,
            "raw_text": "Details opened",
            "message": "Details opened",
            "media": None,
            "reply_markup": None,
        },
    )()
    pre_press_follow_up = type(
        "Message",
        (),
        {
            "id": 8,
            "out": False,
            "raw_text": "This arrived before the press",
            "message": "This arrived before the press",
            "media": None,
            "reply_markup": None,
        },
    )()

    class FakeClient:
        instance = None

        def __init__(self, *_args):
            self.callback_sent = False
            self.minimum_ids: list[int] = []
            self.baseline_reads = 0
            FakeClient.instance = self

        def start(self):
            return None

        def get_entity(self, _username):
            return "weather-bot"

        def get_messages(self, _bot, *, ids=None, min_id=None, limit=None):
            if ids == 7:
                return edited if self.callback_sent else original
            if limit == 1:
                self.baseline_reads += 1
                return [type("Message", (), {"id": 9})()]
            assert min_id is not None
            self.minimum_ids.append(min_id)
            return [pre_press_follow_up] if min_id < 9 else []

        def __call__(self, request):
            assert request.kwargs["msg_id"] == 7
            self.callback_sent = True
            return type("Answer", (), {"message": "opened", "alert": False, "url": None})()

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient, callback_request=Request)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_callback_script(
                "weather_bot",
                7,
                "ZGV0YWlscw==",
                button_text="Details",
                wait_seconds=1,
            ),
            {"__name__": "telegram_callback_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert FakeClient.instance.baseline_reads == 1
    assert FakeClient.instance.minimum_ids == [9, 9]
    assert evidence["replies"] == []
    assert evidence["post_press_message"]["id"] == 7
    assert evidence["post_press_message"]["text"] == "Details opened"


def test_generated_callback_collects_an_interim_and_later_answer(monkeypatch):
    class KeyboardButtonCallback:
        def __init__(self, text, data):
            self.text = text
            self.data = data

    class Row:
        def __init__(self, *buttons):
            self.buttons = buttons

    class ReplyInlineMarkup:
        def __init__(self, *rows):
            self.rows = rows

    class Request:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def bot_message(identifier, text, *, markup=None):
        return type(
            "Message",
            (),
            {
                "id": identifier,
                "out": False,
                "raw_text": text,
                "message": text,
                "media": None,
                "reply_markup": markup,
            },
        )()

    original = bot_message(
        7,
        "Choose",
        markup=ReplyInlineMarkup(Row(KeyboardButtonCallback("Career", b"career"))),
    )
    interim = bot_message(10, "Working on it")
    answer = bot_message(11, "Your career reading")

    class FakeClient:
        def __init__(self, *_args):
            self.callback_sent = False
            self.reply_reads = 0

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def get_messages(self, _bot, *, ids=None, min_id=None, limit=None):
            if ids == 7:
                return original
            if limit == 1:
                return [original]
            assert min_id == 7
            self.reply_reads += 1
            replies = ([interim], [answer, interim], [answer, interim], [answer, interim])
            return replies[self.reply_reads - 1]

        def __call__(self, _request):
            self.callback_sent = True
            return type("Answer", (), {"message": None, "alert": False, "url": None})()

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient, callback_request=Request)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 1, 3, 10))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_callback_script(
                "fortune_bot",
                7,
                "Y2FyZWVy",
                button_text="Career",
                wait_seconds=10,
            ),
            {"__name__": "telegram_callback_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["error"] is None
    assert [reply["text"] for reply in evidence["replies"]] == [
        "Working on it",
        "Your career reading",
    ]


def test_generated_callback_keeps_polling_until_an_interim_edit_is_final(monkeypatch):
    class KeyboardButtonCallback:
        def __init__(self, text, data):
            self.text = text
            self.data = data

    class Row:
        def __init__(self, *buttons):
            self.buttons = buttons

    class ReplyInlineMarkup:
        def __init__(self, *rows):
            self.rows = rows

    class Request:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def bot_message(text, *, markup=None):
        return type(
            "Message",
            (),
            {
                "id": 7,
                "out": False,
                "raw_text": text,
                "message": text,
                "media": None,
                "reply_markup": markup,
            },
        )()

    original = bot_message(
        "Choose",
        markup=ReplyInlineMarkup(Row(KeyboardButtonCallback("Career", b"career"))),
    )
    interim = bot_message("Working on it")
    answer = bot_message("Your career reading")

    class FakeClient:
        def __init__(self, *_args):
            self.callback_sent = False
            self.post_press_reads = 0

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def get_messages(self, _bot, *, ids=None, min_id=None, limit=None):
            if ids == 7:
                if not self.callback_sent:
                    return original
                self.post_press_reads += 1
                return (interim, answer, answer, answer)[self.post_press_reads - 1]
            if limit == 1:
                return [original]
            assert min_id == 7
            return []

        def __call__(self, _request):
            self.callback_sent = True
            return type("Answer", (), {"message": None, "alert": False, "url": None})()

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient, callback_request=Request)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 1, 3, 10))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_callback_script(
                "fortune_bot",
                7,
                "Y2FyZWVy",
                button_text="Career",
                wait_seconds=10,
            ),
            {"__name__": "telegram_callback_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["error"] is None
    assert evidence["post_press_message"]["text"] == "Your career reading"


def test_generated_message_collects_a_later_photo_reply(monkeypatch):
    class MessageMediaPhoto:
        pass

    def bot_message(identifier, text, *, media=None):
        return type(
            "Message",
            (),
            {
                "id": identifier,
                "out": False,
                "raw_text": text,
                "message": text,
                "media": media,
                "reply_markup": None,
            },
        )()

    interim = bot_message(11, "Working on it")
    photo = bot_message(12, "The cards", media=MessageMediaPhoto())

    class FakeClient:
        def __init__(self, *_args):
            self.reply_reads = 0

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def send_message(self, _bot, _message):
            return type("Sent", (), {"id": 10})()

        def get_messages(self, _bot, *, min_id, limit):
            assert (min_id, limit) == (10, 10)
            self.reply_reads += 1
            replies = ([interim], [photo, interim], [photo, interim], [photo, interim])
            return replies[self.reply_reads - 1]

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 1, 3, 10))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_message_script("fortune_bot", "/reading", wait_seconds=10),
            {"__name__": "telegram_probe_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["error"] is None
    assert [reply["id"] for reply in evidence["replies"]] == [11, 12]
    assert evidence["replies"][1]["caption"] == "The cards"
    assert evidence["replies"][1]["media_type"] == "MessageMediaPhoto"


def test_generated_message_keeps_polling_to_the_deadline_after_one_reply(monkeypatch):
    reply = type(
        "Message",
        (),
        {
            "id": 11,
            "out": False,
            "raw_text": "One answer",
            "message": "One answer",
            "media": None,
            "reply_markup": None,
        },
    )()

    class FakeClient:
        instance = None

        def __init__(self, *_args):
            self.reply_reads = 0
            FakeClient.instance = self

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def send_message(self, _bot, _message):
            return type("Sent", (), {"id": 10})()

        def get_messages(self, _bot, *, min_id, limit):
            assert (min_id, limit) == (10, 10)
            self.reply_reads += 1
            return [reply]

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 1, 3, 10))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    sleeps: list[int] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_message_script("fortune_bot", "/reading", wait_seconds=10),
            {"__name__": "telegram_probe_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["replies"][0]["text"] == "One answer"
    assert FakeClient.instance.reply_reads == 4
    assert sleeps == [2, 2, 2]


def test_generated_message_collects_a_reply_well_after_the_old_quiet_window(monkeypatch):
    def bot_message(identifier, text):
        return type(
            "Message",
            (),
            {
                "id": identifier,
                "out": False,
                "raw_text": text,
                "message": text,
                "media": None,
                "reply_markup": None,
            },
        )()

    interim = bot_message(11, "Working on it")
    answer = bot_message(12, "Your career reading")

    class FakeClient:
        def __init__(self, *_args):
            self.reply_reads = 0

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def send_message(self, _bot, _message):
            return type("Sent", (), {"id": 10})()

        def get_messages(self, _bot, *, min_id, limit):
            assert (min_id, limit) == (10, 10)
            self.reply_reads += 1
            replies = ([interim], [interim], [interim], [answer, interim], [answer, interim])
            return replies[self.reply_reads - 1]

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 1, 3, 10, 15))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_message_script("fortune_bot", "/reading", wait_seconds=15),
            {"__name__": "telegram_probe_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["error"] is None
    assert [reply["text"] for reply in evidence["replies"]] == [
        "Working on it",
        "Your career reading",
    ]


def test_generated_message_honors_an_extended_deadline_without_early_exit(monkeypatch):
    def bot_message(identifier, text):
        return type(
            "Message",
            (),
            {
                "id": identifier,
                "out": False,
                "raw_text": text,
                "message": text,
                "media": None,
                "reply_markup": None,
            },
        )()

    interim = bot_message(11, "Working on it")
    answer = bot_message(12, "Your career reading")

    class FakeClient:
        def __init__(self, *_args):
            self.reply_reads = 0

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def send_message(self, _bot, _message):
            return type("Sent", (), {"id": 10})()

        def get_messages(self, _bot, *, min_id, limit):
            assert (min_id, limit) == (10, 10)
            self.reply_reads += 1
            return [answer, interim] if self.reply_reads == 4 else [interim]

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 14, 15, 20))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_message_script("fortune_bot", "/reading", wait_seconds=20),
            {"__name__": "telegram_probe_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["error"] is None
    assert [reply["text"] for reply in evidence["replies"]] == [
        "Working on it",
        "Your career reading",
    ]


def test_generated_message_reads_the_final_state_at_the_deadline(monkeypatch):
    def bot_message(identifier, text):
        return type(
            "Message",
            (),
            {
                "id": identifier,
                "out": False,
                "raw_text": text,
                "message": text,
                "media": None,
                "reply_markup": None,
            },
        )()

    interim = bot_message(11, "Working on it")
    answer = bot_message(12, "Your career reading")

    class FakeClient:
        def __init__(self, *_args):
            self.reply_reads = 0

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def send_message(self, _bot, _message):
            return type("Sent", (), {"id": 10})()

        def get_messages(self, _bot, *, min_id, limit):
            assert (min_id, limit) == (10, 10)
            self.reply_reads += 1
            return [answer, interim] if self.reply_reads == 5 else [interim]

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 1, 3, 14, 15))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_message_script("fortune_bot", "/reading", wait_seconds=15),
            {"__name__": "telegram_probe_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert [reply["text"] for reply in evidence["replies"]] == [
        "Working on it",
        "Your career reading",
    ]


def test_generated_message_keeps_the_newest_replies_at_the_evidence_bound(monkeypatch):
    replies = [
        type(
            "Message",
            (),
            {
                "id": identifier,
                "out": False,
                "raw_text": f"Reply {identifier}",
                "message": f"Reply {identifier}",
                "media": None,
                "reply_markup": None,
            },
        )()
        for identifier in range(11, 23)
    ]

    class FakeClient:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def send_message(self, _bot, _message):
            return type("Sent", (), {"id": 10})()

        def get_messages(self, _bot, *, min_id, limit):
            assert min_id == 10
            # Telethon returns newest-first and applies the requested limit.
            return list(reversed(replies))[:limit]

        def disconnect(self):
            return None

    _install_telethon(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 2, 10))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_message_script("fortune_bot", "/reading", wait_seconds=10),
            {"__name__": "telegram_probe_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert [reply["id"] for reply in evidence["replies"]] == list(range(13, 23))


def _install_telethon(monkeypatch, client, *, callback_request=None):
    """Install only the generated script's Telethon import surface."""
    telethon = ModuleType("telethon")
    telethon.__path__ = []
    sync = ModuleType("telethon.sync")
    sync.TelegramClient = client
    sessions = ModuleType("telethon.sessions")
    sessions.StringSession = lambda value: value
    telethon.sync = sync
    telethon.sessions = sessions
    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    if callback_request is not None:
        tl = ModuleType("telethon.tl")
        tl.__path__ = []
        functions = ModuleType("telethon.tl.functions")
        functions.__path__ = []
        messages = ModuleType("telethon.tl.functions.messages")
        messages.GetBotCallbackAnswerRequest = callback_request
        tl.functions = functions
        functions.messages = messages
        telethon.tl = tl
        monkeypatch.setitem(sys.modules, "telethon.tl", tl)
        monkeypatch.setitem(sys.modules, "telethon.tl.functions", functions)
        monkeypatch.setitem(sys.modules, "telethon.tl.functions.messages", messages)
