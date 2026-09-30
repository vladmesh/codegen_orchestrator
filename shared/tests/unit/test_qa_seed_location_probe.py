"""The Telegram location seed probe: values in, values out, never source.

PR #609's location transport interpolated the coordinates unquoted into
generated Python. The seed parses LAT and LON with `float()`, refuses anything
that is not a finite in-range number before Telethon is even imported, and hands
Telethon the values themselves.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import types

import pytest

from shared.contracts.dto.run_result import QAProbeFileKind, QAProbePlatform
from shared.qa_probe_cli import QA_PROBE_LIBRARY_PATH
from shared.qa_probe_library import SEED_ORIGIN, seed_probes
from shared.qa_probe_library.telegram import location

TELETHON_MODULES = (
    "telethon",
    "telethon.sync",
    "telethon.sessions",
    "telethon.tl",
    "telethon.tl.types",
)
IDENTITY = {
    "api_id": 12345,
    "api_hash": "api-hash-value",
    "session": "string-session-value",
    "user_id": 777,
    "proxy": ["http", "qa-egress-proxy", 3128],
}


@dataclass
class InputGeoPoint:
    lat: object
    long: object


@dataclass
class InputMediaGeoPoint:
    geo_point: InputGeoPoint


@dataclass
class Message:
    id: int
    out: bool
    raw_text: str | None = None
    media: object = None
    reply_markup: object = None


class FakeTelegram:
    """Everything the probe asked Telethon to do, in order."""

    def __init__(self, *, authorized: bool = True, send_error: Exception | None = None):
        self.calls: list[tuple] = []
        self.authorized = authorized
        self.send_error = send_error
        fake = self

        class TelegramClient:
            def __init__(self, session, api_id, api_hash, *, proxy=None):
                fake.calls.append(("client", session, api_id, api_hash, proxy))

            def connect(self):
                fake.calls.append(("connect",))

            def is_user_authorized(self):
                return fake.authorized

            def get_entity(self, name):
                fake.calls.append(("get_entity", name))
                return f"entity:{name}"

            def send_file(self, entity, media):
                fake.calls.append(("send_file", entity, media))
                if fake.send_error is not None:
                    raise fake.send_error
                return Message(id=100, out=True)

            def get_messages(self, entity, *, min_id, limit):
                fake.calls.append(("get_messages", entity, min_id, limit))
                # Newest first, as Telethon answers; the probe's own message is out.
                return [
                    Message(id=102, out=False, raw_text="Moscow: +12°C"),
                    Message(id=101, out=False, raw_text="Location received"),
                    Message(id=100, out=True),
                ]

            def disconnect(self):
                fake.calls.append(("disconnect",))

        self.modules = {
            "telethon": types.ModuleType("telethon"),
            "telethon.sync": types.ModuleType("telethon.sync"),
            "telethon.sessions": types.ModuleType("telethon.sessions"),
            "telethon.tl": types.ModuleType("telethon.tl"),
            "telethon.tl.types": types.ModuleType("telethon.tl.types"),
        }
        self.modules["telethon.sync"].TelegramClient = TelegramClient
        self.modules["telethon.sessions"].StringSession = lambda value: ("StringSession", value)
        self.modules["telethon.tl.types"].InputGeoPoint = InputGeoPoint
        self.modules["telethon.tl.types"].InputMediaGeoPoint = InputMediaGeoPoint


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_identity(home: Path, identity: dict | str) -> None:
    (home / ".qa").mkdir(exist_ok=True)
    body = identity if isinstance(identity, str) else json.dumps(identity)
    (home / ".qa" / "telegram_identity.json").write_text(body)


@pytest.fixture
def telegram(monkeypatch):
    fake = FakeTelegram()
    for name, module in fake.modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return fake


@pytest.fixture
def no_telethon(monkeypatch):
    """Telethon cannot be imported at all: any attempt fails the test loudly."""
    for name in TELETHON_MODULES:
        monkeypatch.setitem(sys.modules, name, None)


def test_valid_coordinates_reach_telethon_as_floats_and_replies_print_as_json(
    home, telegram, capsys
):
    _write_identity(home, IDENTITY)

    status = location.main(["@weather_bot", "55.7558", "-37.6173", "0"])

    out = capsys.readouterr().out
    assert status == 0
    [client] = [call for call in telegram.calls if call[0] == "client"]
    assert client == (
        "client",
        ("StringSession", "string-session-value"),
        12345,
        "api-hash-value",
        ("http", "qa-egress-proxy", 3128),
    )
    [send] = [call for call in telegram.calls if call[0] == "send_file"]
    assert send[1] == "entity:@weather_bot"
    point = send[2].geo_point
    assert (point.lat, point.long) == (55.7558, -37.6173)
    assert type(point.lat) is float and type(point.long) is float
    assert telegram.calls[-1] == ("disconnect",)
    result = json.loads(out)
    assert result["delivered"] is True
    assert result["error"] is None
    assert result["sent_message_id"] == 100
    assert [reply["text"] for reply in result["replies"]] == [
        "Location received",
        "Moscow: +12°C",
    ]


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        ("0", "0"),
        ("90", "180"),
        ("-90", "-180"),
        ("1e1", "-2.5"),
    ],
)
def test_boundary_and_exponent_coordinates_are_accepted(home, telegram, lat, lon):
    _write_identity(home, IDENTITY)

    assert location.main(["weather_bot", lat, lon, "0"]) == 0

    [send] = [call for call in telegram.calls if call[0] == "send_file"]
    assert (send[2].geo_point.lat, send[2].geo_point.long) == (float(lat), float(lon))


@pytest.mark.parametrize(
    ("lat", "lon"),
    [
        ("nan", "37.6"),
        ("55.7", "NaN"),
        ("inf", "37.6"),
        ("55.7", "-inf"),
        ("-Infinity", "37.6"),
        ("90.0001", "37.6"),
        ("-91", "37.6"),
        ("55.7", "180.5"),
        ("55.7", "-181"),
        ("north", "37.6"),
        ("55.7", ""),
        ("55,7", "37.6"),
        ("0x1p3", "37.6"),
        ("1); import os; os.system('x')#", "37.6"),
        ("55.7", "1); import os; os.system('x')#"),
        ("__import__('os').system('x')", "37.6"),
    ],
)
def test_bad_coordinates_are_refused_before_any_telethon_call(home, no_telethon, capsys, lat, lon):
    _write_identity(home, IDENTITY)

    status = location.main(["@weather_bot", lat, lon])

    captured = capsys.readouterr()
    assert status == location.EXIT_ARGUMENT_REFUSED
    assert captured.out == ""
    assert "location probe refused" in captured.err
    assert "LAT" in captured.err or "LON" in captured.err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["@weather_bot", "55.7"],
        ["@weather_bot", "55.7", "37.6", "5", "extra"],
        ["weather bot", "55.7", "37.6"],
        ["@x", "55.7", "37.6"],
        ["@weather_bot\nimport os", "55.7", "37.6"],
        ["@weather_bot", "55.7", "37.6", "-1"],
        ["@weather_bot", "55.7", "37.6", "41"],
        ["@weather_bot", "55.7", "37.6", "1.5"],
    ],
)
def test_malformed_arguments_are_refused_before_any_telethon_call(home, no_telethon, argv):
    _write_identity(home, IDENTITY)

    assert location.main(argv) == location.EXIT_ARGUMENT_REFUSED


@pytest.mark.parametrize(
    ("identity", "detail"),
    [
        (None, "run `qa telegram_identity` first"),
        ("{not json", "unreadable"),
        ("[]", "not an object"),
        ({**IDENTITY, "session": ""}, "no session"),
        ({**IDENTITY, "api_id": "12345"}, "no api_id"),
        ({key: value for key, value in IDENTITY.items() if key != "api_hash"}, "no api_hash"),
        ({**IDENTITY, "proxy": None}, "carries no proxy"),
    ],
)
def test_a_missing_or_unproven_identity_is_a_clear_nonzero_exit(
    home, no_telethon, capsys, identity, detail
):
    if identity is not None:
        _write_identity(home, identity)

    status = location.main(["@weather_bot", "55.7", "37.6"])

    captured = capsys.readouterr()
    assert status == location.EXIT_NO_IDENTITY
    assert detail in captured.err
    assert captured.out == ""


def test_an_unauthorized_session_sends_nothing_and_fails(home, telegram, capsys):
    telegram.authorized = False
    _write_identity(home, IDENTITY)

    status = location.main(["@weather_bot", "55.7", "37.6", "0"])

    result = json.loads(capsys.readouterr().out)
    assert status == location.EXIT_TELEGRAM_FAILED
    assert result["delivered"] is False
    assert "not authorized" in result["error"]
    assert not [call for call in telegram.calls if call[0] == "send_file"]
    assert telegram.calls[-1] == ("disconnect",)


def test_a_telegram_send_failure_is_reported_and_nonzero(home, telegram, capsys):
    telegram.send_error = ConnectionError("proxy refused")
    _write_identity(home, IDENTITY)

    status = location.main(["@weather_bot", "55.7", "37.6", "0"])

    result = json.loads(capsys.readouterr().out)
    assert status == location.EXIT_TELEGRAM_FAILED
    assert result["delivered"] is False
    assert result["error"] == "ConnectionError: proxy refused"


def test_the_probe_generates_and_evaluates_no_source():
    tree = ast.parse(Path(location.__file__).read_text(encoding="utf-8"))

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not called & {"exec", "eval", "compile", "__import__"}
    assert imported <= {"base64", "json", "math", "os", "re", "sys", "time", "telethon"}
    assert "system" not in {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_the_location_seed_is_offered_only_to_runs_with_a_telegram_bot():
    assert seed_probes(telegram_bot=False) == []
    [seed] = seed_probes(telegram_bot=True)

    assert seed.platform is QAProbePlatform.TELEGRAM
    assert seed.name == "location"
    assert seed.file_kind is QAProbeFileKind.PY
    assert seed.relative_path == "telegram/location.py"
    assert seed.usage.startswith(
        f"qa probe telegram location {QA_PROBE_LIBRARY_PATH}/telegram/location.py @BOT LAT LON"
    )
    assert seed.source() == Path(location.__file__).read_text(encoding="utf-8")
    assert SEED_ORIGIN == "seed"
