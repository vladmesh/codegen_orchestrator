"""Telegram probe wire-format regressions."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest

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
    _virtual_clock(monkeypatch)

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
    _virtual_clock(monkeypatch)

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


class MessageMediaPhoto:
    pass


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


class CallbackRequest:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Clock:
    """Virtual monotonic time that the generated script reads and advances by sleeping."""

    def __init__(self):
        self.now = 0
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.now += seconds


def _virtual_clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    return clock


def _bot_message(identifier, text, *, media=None, markup=None):
    return SimpleNamespace(
        id=identifier, out=False, raw_text=text, message=text, media=media, reply_markup=markup
    )


def _message_bot(clock, chat):
    """A bot whose replies after the probe's message 10 are ``chat(now)``, oldest first."""

    class Client:
        reads: list[float] = []

        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def send_message(self, _bot, _message):
            return SimpleNamespace(id=10)

        def get_messages(self, _bot, *, min_id, limit):
            assert min_id == 10
            Client.reads.append(clock.now)
            return list(reversed(chat(clock.now)))[:limit]

        def disconnect(self):
            return None

    return Client


def _probe(monkeypatch, client, *, wait_seconds) -> dict:
    _install_telethon(monkeypatch, client)
    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_message_script("fortune_bot", "/reading", wait_seconds=wait_seconds),
            {"__name__": "telegram_probe_script"},
        )
    return parse_bot_probe_result(stdout.getvalue())


def _callback_bot(clock, *, pressed, chat, answer=None):
    """Message 7 offers one button; after the press it reads ``pressed(now)``.

    The newest message before the press is 9; ``chat(now)`` is what follows it.
    """
    original = _bot_message(
        7, "Choose", markup=ReplyInlineMarkup(Row(KeyboardButtonCallback("Career", b"career")))
    )

    class Client:
        reads: list[float] = []

        def __init__(self, *_args):
            self.pressed = False

        def start(self):
            return None

        def get_entity(self, _username):
            return "fortune-bot"

        def get_messages(self, _bot, *, ids=None, min_id=None, limit=None):
            if ids == 7:
                return pressed(clock.now) if self.pressed else original
            if limit == 1:
                return [SimpleNamespace(id=9)]
            assert min_id == 9
            Client.reads.append(clock.now)
            return list(reversed(chat(clock.now)))

        def __call__(self, request):
            assert request.kwargs == {"peer": "fortune-bot", "msg_id": 7, "data": b"career"}
            self.pressed = True
            return SimpleNamespace(message=answer, alert=False, url=None)

        def disconnect(self):
            return None

    return Client, original


def _press(monkeypatch, client, *, wait_seconds) -> dict:
    _install_telethon(monkeypatch, client, callback_request=CallbackRequest)
    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 - generated probe source
            build_bot_callback_script(
                "fortune_bot", 7, "Y2FyZWVy", button_text="Career", wait_seconds=wait_seconds
            ),
            {"__name__": "telegram_callback_script"},
        )
    return parse_bot_probe_result(stdout.getvalue())


def test_generated_message_returns_once_one_reply_stays_unchanged_for_the_quiet_window(
    monkeypatch,
):
    clock = _virtual_clock(monkeypatch)
    client = _message_bot(clock, lambda _now: [_bot_message(11, "One answer")])

    evidence = _probe(monkeypatch, client, wait_seconds=15)

    assert [reply["text"] for reply in evidence["replies"]] == ["One answer"]
    assert client.reads == [0, 2, 3]
    assert clock.sleeps == [2, 1]


def test_generated_message_silence_runs_to_the_deadline_and_reads_it(monkeypatch):
    clock = _virtual_clock(monkeypatch)
    client = _message_bot(clock, lambda _now: [])

    evidence = _probe(monkeypatch, client, wait_seconds=15)

    assert evidence["delivered"] is True
    assert evidence["replies"] == []
    assert client.reads == [0, 2, 4, 6, 8, 10, 12, 14, 15]
    assert clock.now == 15


def test_generated_message_successive_replies_reset_the_quiet_window(monkeypatch):
    clock = _virtual_clock(monkeypatch)
    interim = _bot_message(11, "Working on it")
    photo = _bot_message(12, "The cards", media=MessageMediaPhoto())
    answer = _bot_message(13, "Your reading")
    client = _message_bot(
        clock, lambda now: [interim, photo, answer][: 1 + (now >= 2) + (now >= 4)]
    )

    evidence = _probe(monkeypatch, client, wait_seconds=60)

    assert [reply["id"] for reply in evidence["replies"]] == [11, 12, 13]
    assert evidence["replies"][1]["caption"] == "The cards"
    assert evidence["replies"][1]["media_type"] == "MessageMediaPhoto"
    assert client.reads == [0, 2, 4, 6, 7]


def test_generated_message_edits_of_text_media_and_markup_reset_the_quiet_window(monkeypatch):
    clock = _virtual_clock(monkeypatch)

    def chat(now):
        if now < 2:
            return [_bot_message(11, "Loading")]
        markup = ReplyInlineMarkup(
            Row(KeyboardButtonCallback("Again" if now >= 6 else "More", b"more"))
        )
        if now < 4:
            return [_bot_message(11, "The cards", media=MessageMediaPhoto())]
        return [_bot_message(11, "The cards", media=MessageMediaPhoto(), markup=markup)]

    client = _message_bot(clock, chat)

    evidence = _probe(monkeypatch, client, wait_seconds=60)

    (reply,) = evidence["replies"]
    assert reply["caption"] == "The cards"
    assert reply["reply_markup"]["buttons"][0]["text"] == "Again"
    assert client.reads == [0, 2, 4, 6, 8, 9]


def test_generated_message_does_not_collect_a_reply_after_a_completed_quiet_window(
    monkeypatch,
):
    clock = _virtual_clock(monkeypatch)
    interim = _bot_message(11, "Working on it")
    answer = _bot_message(12, "Your reading")
    client = _message_bot(clock, lambda now: [interim, answer] if now >= 10 else [interim])

    evidence = _probe(monkeypatch, client, wait_seconds=15)

    assert [reply["text"] for reply in evidence["replies"]] == ["Working on it"]
    assert clock.now == 3


def test_generated_message_short_wait_ends_before_the_quiet_window(monkeypatch):
    clock = _virtual_clock(monkeypatch)
    client = _message_bot(clock, lambda _now: [_bot_message(11, "One answer")])

    evidence = _probe(monkeypatch, client, wait_seconds=1)

    assert [reply["id"] for reply in evidence["replies"]] == [11]
    assert client.reads == [0, 1]
    assert clock.sleeps == [1]


def test_generated_message_replies_changing_through_the_deadline_are_read_at_it(monkeypatch):
    clock = _virtual_clock(monkeypatch)
    client = _message_bot(
        clock,
        lambda now: [_bot_message(identifier, "tick") for identifier in range(11, 12 + int(now))],
    )

    evidence = _probe(monkeypatch, client, wait_seconds=5)

    assert [reply["id"] for reply in evidence["replies"]] == list(range(11, 17))
    assert client.reads == [0, 2, 4, 5]


def test_generated_message_keeps_the_newest_replies_at_the_evidence_bound(monkeypatch):
    clock = _virtual_clock(monkeypatch)
    replies = [_bot_message(identifier, f"Reply {identifier}") for identifier in range(11, 23)]
    client = _message_bot(clock, lambda _now: replies)

    evidence = _probe(monkeypatch, client, wait_seconds=10)

    assert [reply["id"] for reply in evidence["replies"]] == list(range(13, 23))


def test_generated_callback_replies_after_the_press_settle_before_the_deadline(monkeypatch):
    clock = _virtual_clock(monkeypatch)
    interim = _bot_message(10, "Working on it")
    answer = _bot_message(11, "Your career reading")
    client, original = _callback_bot(
        clock,
        pressed=lambda _now: original,
        chat=lambda now: [interim, answer] if now >= 2 else [interim],
    )

    evidence = _press(monkeypatch, client, wait_seconds=10)

    assert evidence["error"] is None
    assert [reply["text"] for reply in evidence["replies"]] == [
        "Working on it",
        "Your career reading",
    ]
    assert client.reads == [0, 2, 4, 5]


def test_generated_callback_edits_of_the_pressed_message_reset_the_quiet_window(monkeypatch):
    clock = _virtual_clock(monkeypatch)
    client, _original = _callback_bot(
        clock,
        pressed=lambda now: _bot_message(7, "Your career reading" if now >= 2 else "Working on it"),
        chat=lambda _now: [],
    )

    evidence = _press(monkeypatch, client, wait_seconds=10)

    assert evidence["error"] is None
    assert evidence["replies"] == []
    assert evidence["post_press_message"]["text"] == "Your career reading"
    assert client.reads == [0, 2, 4, 5]


def test_generated_callback_settles_after_an_edit_restores_the_original_message(monkeypatch):
    """A response once observed still qualifies after the pressed message reverts."""
    clock = _virtual_clock(monkeypatch)
    client, original = _callback_bot(
        clock,
        pressed=lambda now: original if now >= 2 else _bot_message(7, "Loading"),
        chat=lambda _now: [],
    )

    evidence = _press(monkeypatch, client, wait_seconds=60)

    assert evidence["error"] is None
    assert evidence["post_press_message"]["text"] == "Choose"
    assert evidence["post_press_message"]["reply_markup"]["buttons"][0]["text"] == "Career"
    assert client.reads == [0, 2, 4, 5]
    assert clock.now == 5


@pytest.mark.parametrize("answer", [None, "Loading your reading"])
def test_generated_callback_acknowledgement_alone_runs_to_the_deadline(monkeypatch, answer):
    """An unchanged pressed message and a callback answer are not a bot response."""
    clock = _virtual_clock(monkeypatch)
    client, original = _callback_bot(
        clock, pressed=lambda _now: original, chat=lambda _now: [], answer=answer
    )

    evidence = _press(monkeypatch, client, wait_seconds=10)

    assert evidence["delivered"] is True
    assert evidence["callback"] == {"text": answer, "alert": False, "url": None}
    assert evidence["post_press_message"]["text"] == "Choose"
    assert client.reads == [0, 2, 4, 6, 8, 10]


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
