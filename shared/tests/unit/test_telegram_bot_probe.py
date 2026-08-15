"""Telegram probe wire-format regressions."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import sys
from types import ModuleType

from shared.telegram_bot_probe import (
    PROBE_RESULT_MARKER,
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
