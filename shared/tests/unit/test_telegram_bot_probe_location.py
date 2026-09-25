"""Tests for the location probe script and build_bot_location_script.

These tests cover:
- Script generation (build_bot_location_script)
- Script execution against a fake Telethon client
- CLI parsing for telegram_send_location
"""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import sys
import time
from types import ModuleType

from shared.telegram_bot_probe import (
    PROBE_RESULT_MARKER,
    build_bot_location_script,
    parse_bot_probe_result,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_telethon_with_types(monkeypatch, client, *, geo_point_cls=None, geo_media_cls=None):
    """Install the Telethon import surface needed by the location probe script."""
    telethon = ModuleType("telethon")
    telethon.__path__ = []
    sync = ModuleType("telethon.sync")
    sync.TelegramClient = client
    sessions = ModuleType("telethon.sessions")
    sessions.StringSession = lambda value: value
    telethon.sync = sync
    telethon.sessions = sessions

    # telethon.tl.types — needed for InputMediaGeoPoint and InputGeoPoint
    tl = ModuleType("telethon.tl")
    tl.__path__ = []
    types_mod = ModuleType("telethon.tl.types")

    class _DefaultInputGeoPoint:
        def __init__(self, *, lat, long):
            self.lat = lat
            self.long = long

    class _DefaultInputMediaGeoPoint:
        def __init__(self, *, geo_point):
            self.geo_point = geo_point

    types_mod.InputGeoPoint = geo_point_cls or _DefaultInputGeoPoint
    types_mod.InputMediaGeoPoint = geo_media_cls or _DefaultInputMediaGeoPoint
    tl.types = types_mod
    telethon.tl = tl

    monkeypatch.setitem(sys.modules, "telethon", telethon)
    monkeypatch.setitem(sys.modules, "telethon.sync", sync)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions)
    monkeypatch.setitem(sys.modules, "telethon.tl", tl)
    monkeypatch.setitem(sys.modules, "telethon.tl.types", types_mod)


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------


def test_script_contains_bot_username_and_coordinates():
    script = build_bot_location_script("weather_bot", 55.7558, 37.6176)
    assert "@weather_bot" in script
    assert "55.7558" in script
    assert "37.6176" in script


def test_script_uses_input_media_geo_point():
    script = build_bot_location_script("weather_bot", 55.7558, 37.6176)
    assert "InputMediaGeoPoint" in script
    assert "InputGeoPoint" in script


def test_script_prints_probe_result_marker():
    script = build_bot_location_script("weather_bot", 55.7558, 37.6176)
    assert PROBE_RESULT_MARKER in script


def test_script_action_is_location():
    script = build_bot_location_script("weather_bot", 55.7558, 37.6176)
    assert "'action': 'location'" in script


# ---------------------------------------------------------------------------
# Script execution
# ---------------------------------------------------------------------------


def test_location_script_delivers_and_collects_reply(monkeypatch):
    """Happy path: location is sent, bot replies."""

    reply = type(
        "Message",
        (),
        {
            "id": 20,
            "out": False,
            "raw_text": "Получил геолокацию и проверяю текущую погоду.",
            "message": "Получил геолокацию и проверяю текущую погоду.",
            "media": None,
            "reply_markup": None,
        },
    )()

    class FakeClient:
        instance = None

        def __init__(self, *_args):
            self.sent_geo = None
            FakeClient.instance = self

        def start(self):
            return None

        def get_entity(self, _username):
            return "weather-bot"

        def send_file(self, _bot, geo):
            self.sent_geo = geo
            return type("Sent", (), {"id": 19})()

        def get_messages(self, _bot, *, min_id, limit):
            return [reply] if min_id == 19 else []

        def disconnect(self):
            return None

    _install_telethon_with_types(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 — generated probe source
            build_bot_location_script("weather_bot", 55.7558, 37.6176, wait_seconds=1),
            {"__name__": "telegram_location_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["action"] == "location"
    assert evidence["delivered"] is True
    assert evidence["error"] is None
    assert evidence["replies"][0]["text"] == "Получил геолокацию и проверяю текущую погоду."
    # The geo object was passed correctly
    sent_geo = FakeClient.instance.sent_geo
    assert sent_geo.geo_point.lat == 55.7558
    assert sent_geo.geo_point.long == 37.6176


def test_location_script_records_send_failure_as_undelivered(monkeypatch):
    """If send_file raises, delivered is False and error is set."""

    class FakeClient:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def get_entity(self, _username):
            return "weather-bot"

        def send_file(self, _bot, _geo):
            raise OSError("network error")

        def disconnect(self):
            return None

    _install_telethon_with_types(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 — generated probe source
            build_bot_location_script("weather_bot", 55.7558, 37.6176, wait_seconds=1),
            {"__name__": "telegram_location_script"},
        )

    evidence = parse_bot_probe_result(stdout.getvalue())

    assert evidence["delivered"] is False
    assert "OSError" in evidence["error"]
    assert evidence["replies"] == []


def test_location_script_uses_sent_message_id_as_reply_baseline(monkeypatch):
    """Replies are collected using the sent message's id as min_id."""

    seen_min_ids: list[int] = []

    class FakeClient:
        def __init__(self, *_args):
            pass

        def start(self):
            return None

        def get_entity(self, _username):
            return "weather-bot"

        def send_file(self, _bot, _geo):
            return type("Sent", (), {"id": 42})()

        def get_messages(self, _bot, *, min_id, limit):
            seen_min_ids.append(min_id)
            return []

        def disconnect(self):
            return None

    _install_telethon_with_types(monkeypatch, FakeClient)
    monkeypatch.setenv("TELETHON_SESSION", "session")
    monkeypatch.setenv("TELETHON_API_ID", "123")
    monkeypatch.setenv("TELETHON_API_HASH", "hash")
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    stdout = StringIO()
    with redirect_stdout(stdout):
        exec(  # noqa: S102 — generated probe source
            build_bot_location_script("weather_bot", 0.0, 0.0, wait_seconds=1),
            {"__name__": "telegram_location_script"},
        )

    parse_bot_probe_result(stdout.getvalue())
    assert all(mid == 42 for mid in seen_min_ids)


# ---------------------------------------------------------------------------
# qa_probe_cli — CLI and usage strings
# ---------------------------------------------------------------------------


def test_cli_usage_documents_telegram_send_location():
    from shared.qa_probe_cli import QA_PROBE_USAGE

    assert "telegram_send_location" in QA_PROBE_USAGE


def test_qa_probe_script_contains_telegram_send_location_command():
    from shared.qa_probe_cli import QA_PROBE_SCRIPT

    assert "telegram_send_location" in QA_PROBE_SCRIPT
