"""The fixed Telegram tools' scripts carry values, never source.

`telegram_probe` and `telegram_click_button` stay platform tools in qa-worker
(sprint:1464 option B). Their child scripts are generated here with every input
as a JSON literal, so no input can become code. This proves it by execution: each
script is built with hostile values, run by the production `run_probe_script` in
a child process against a stub Telethon on the child's `PYTHONPATH`, and the stub
must have received exactly those values, byte for byte, and nothing else.

Every payload that would run if it escaped its literal sets a flag the test owns:
`__import__('telethon').injected(...)` writes it from the stub, and `os.system`
finds an `x` on the child's `PATH` that writes it too. The flag stays unset.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from shared.telegram_access_probe import run_probe_script
from shared.telegram_bot_probe import (
    build_bot_callback_script,
    build_bot_message_script,
    parse_bot_probe_result,
)

MESSAGE_ID = 41
# The id the stub gives the probe's own sent message.
SENT_ID = 100

# What the stub records, one JSON event per line; and the flag an escaped
# payload would set. Both paths reach the child only through its environment.
STUB_TELETHON = {
    "telethon/__init__.py": """
import json, os


def record(*event):
    with open(os.environ["STUB_TELETHON_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(list(event)) + "\\n")


def injected(*_args):
    with open(os.environ["STUB_INJECTION_FLAG"], "a", encoding="utf-8") as handle:
        handle.write("telethon.injected\\n")
""",
    "telethon/sessions.py": """
class StringSession:
    def __init__(self, value):
        self.value = value
""",
    "telethon/sync.py": """
import base64, os
from telethon import record


class Message:
    def __init__(self, id, out, raw_text=None, reply_markup=None):
        self.id = id
        self.out = out
        self.raw_text = raw_text
        self.message = raw_text
        self.media = None
        self.reply_markup = reply_markup


class Button:
    def __init__(self, text, data):
        self.text = text
        self.data = data


class Row:
    def __init__(self, buttons):
        self.buttons = buttons


class ReplyInlineMarkup:
    def __init__(self, rows):
        self.rows = rows


def _button_data():
    return base64.b64decode(os.environ["STUB_BUTTON_DATA"])


class Answer:
    message = "ok"
    alert = False
    url = None


class TelegramClient:
    def __init__(self, session, api_id, api_hash):
        record("client", session.value, api_id, api_hash)

    def start(self):
        record("start")

    def get_entity(self, name):
        record("get_entity", name)
        return "bot-entity"

    def send_message(self, entity, text):
        record("send_message", entity, text)
        return Message(100, True)

    def get_messages(self, entity, ids=None, min_id=None, limit=None):
        if ids is not None:
            record("get_messages", entity, "ids", ids)
            markup = ReplyInlineMarkup([Row([Button("Details", _button_data())])])
            return Message(ids, False, "Choose", markup)
        record("get_messages", entity, "min_id", min_id, limit)
        return []

    def __call__(self, request):
        record(
            "callback",
            request.peer,
            request.msg_id,
            base64.b64encode(request.data).decode("ascii"),
        )
        return Answer()

    def disconnect(self):
        record("disconnect")
""",
    "telethon/tl/__init__.py": "",
    "telethon/tl/functions/__init__.py": "",
    "telethon/tl/functions/messages.py": """
class GetBotCallbackAnswerRequest:
    def __init__(self, peer, msg_id, data):
        self.peer = peer
        self.msg_id = msg_id
        self.data = data
""",
}

# Every value goes into every slot: the message text, the button text, the
# callback data and the bot username.
HOSTILE = {
    "double-quote": '"',
    "single-quote": "'",
    "triple-double": '"""',
    "triple-single": "'''",
    "backslashes": "\\ \\\\ \\n \\x00 \\u0041 \\",
    "newlines": "line one\nline two\r\nline three\n",
    "nul": "before\x00after",
    "unicode-separators": "a b c\u0085d﻿e​f\t",
    "braces": "{} {0} {message} }}{{",
    "percent": "%s %d %(message)s %%",
    "dollar": "${} ${HOME} $HOME",
    "os-system": "__import__('os').system('x')",
    "close-and-call": '"); import os; os.system("x"); ("',
    "close-single-and-call": "'); import os; os.system('x'); ('",
    "close-triple-and-flag": '"""); __import__(\'telethon\').injected(); ("""',
    "flag-expression": "__import__('telethon').injected('expression')",
    "comment-and-newline": "# \n__import__('telethon').injected('newline')\n",
    "long": ("0123456789abcdef" * 250)[:4000],
    "non-bmp": "𝄞 😀 𐍈 🏳️‍🌈 \U0010ffff",
}


@pytest.fixture
def child(tmp_path):
    """The stub Telethon package, the flag, and the child's environment."""
    stub = tmp_path / "stub"
    for relative, source in STUB_TELETHON.items():
        path = stub / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    flag = tmp_path / "injected.flag"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # What `os.system('x')` would run if a payload ever became code.
    command = bin_dir / "x"
    command.write_text(f"#!/bin/sh\necho os.system >> '{flag}'\n")
    command.chmod(0o755)
    log = tmp_path / "telethon.log"

    def environment(button_data: bytes = b"") -> dict[str, str]:
        log.unlink(missing_ok=True)
        return {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(stub),
            "STUB_TELETHON_LOG": str(log),
            "STUB_INJECTION_FLAG": str(flag),
            "STUB_BUTTON_DATA": base64.b64encode(button_data).decode("ascii"),
            "TELETHON_SESSION": "stub-session",
            "TELETHON_API_ID": "12345",
            "TELETHON_API_HASH": "stub-api-hash",
        }

    def events() -> list[list]:
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return SimpleNamespace(environment=environment, events=events, flag=flag)


async def _run(script: str, env: dict[str, str]) -> dict:
    run = await run_probe_script(script, env=env, timeout=30, python_bin=sys.executable)
    assert run.exit_status == 0, run.stderr
    assert run.stderr == ""
    markers = [line for line in run.stdout.splitlines() if line]
    assert len(markers) == 1, run.stdout
    return parse_bot_probe_result(run.stdout)


def _same(received: str, value: str) -> None:
    assert received == value
    assert received.encode("utf-8") == value.encode("utf-8")


@pytest.mark.parametrize("value", list(HOSTILE.values()), ids=list(HOSTILE))
async def test_a_hostile_message_and_bot_username_reach_telethon_as_values(child, value):
    script = build_bot_message_script(value, value, wait_seconds=0)

    result = await _run(script, child.environment())
    assert not child.flag.exists(), child.flag.read_text()

    events = child.events()
    assert [event[0] for event in events] == [
        "client",
        "start",
        "get_entity",
        "send_message",
        "get_messages",
        "disconnect",
    ]
    assert events[0] == ["client", "stub-session", 12345, "stub-api-hash"]
    _same(events[2][1], "@" + value)
    assert events[3][1] == "bot-entity"
    _same(events[3][2], value)
    assert events[4] == ["get_messages", "bot-entity", "min_id", SENT_ID, 10]
    assert result["error"] is None
    assert result["delivered"] is True
    _same(result["sent"], value)
    _same(result["attempted"], f"send {value!r} to @{value}")


@pytest.mark.parametrize("value", list(HOSTILE.values()), ids=list(HOSTILE))
async def test_a_hostile_button_press_reaches_telethon_as_values(child, value):
    """The bot's own button data is hostile bytes; the press sends exactly those bytes."""
    data = value.encode("utf-8")
    callback_data = base64.b64encode(data).decode("ascii")
    script = build_bot_callback_script(
        value, MESSAGE_ID, callback_data, button_text=value, wait_seconds=0
    )

    result = await _run(script, child.environment(button_data=data))
    assert not child.flag.exists(), child.flag.read_text()

    events = child.events()
    assert [event[0] for event in events] == [
        "client",
        "start",
        "get_entity",
        "get_messages",
        "get_messages",
        "callback",
        "get_messages",
        "get_messages",
        "disconnect",
    ]
    _same(events[2][1], "@" + value)
    assert events[3] == ["get_messages", "bot-entity", "ids", MESSAGE_ID]
    assert events[5] == ["callback", "bot-entity", MESSAGE_ID, callback_data]
    assert base64.b64decode(events[5][3]) == data
    assert result["error"] is None
    assert result["delivered"] is True
    _same(result["attempted"], f"press {value}")
    _same(result["sent"], f"message_id={MESSAGE_ID} callback_data={callback_data}")


@pytest.mark.parametrize("value", list(HOSTILE.values()), ids=list(HOSTILE))
async def test_hostile_callback_data_stays_a_value_and_presses_nothing(child, value):
    """Callback data that is not a visible button's is compared as a value and refused."""
    script = build_bot_callback_script(value, MESSAGE_ID, value, button_text=value, wait_seconds=0)

    result = await _run(script, child.environment(button_data=b"details"))
    assert not child.flag.exists(), child.flag.read_text()

    events = child.events()
    assert [event[0] for event in events] == [
        "client",
        "start",
        "get_entity",
        "get_messages",
        "disconnect",
    ]
    _same(events[2][1], "@" + value)
    assert result["delivered"] is False
    assert result["error"] == (
        "ValueError: the requested callback is not visible on that bot reply"
    )
    _same(result["sent"], f"message_id={MESSAGE_ID} callback_data={value}")
    _same(result["attempted"], f"press {value}")


async def test_the_flag_is_reachable_so_its_absence_means_something(child):
    """The canary: a script that does run the payloads sets the flag both ways."""
    script = (
        "import os\n"
        f"exec({json.dumps(HOSTILE['flag-expression'])})\n"
        f"exec({json.dumps(HOSTILE['os-system'])})\n"
    )

    run = await run_probe_script(script, env=child.environment(), timeout=30)

    assert run.exit_status == 0, run.stderr
    assert Path(child.flag).read_text().splitlines() == ["telethon.injected", "os.system"]
