"""The central executor's serialized QA capability command."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import urllib.request

import pytest

from shared.qa_probe_cli import QA_PROBE_SCRIPT


class _Response:
    def __init__(self, body: str) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body.encode("utf-8")


@pytest.mark.parametrize(
    ("argv", "answer", "expected_exit", "expected_call"),
    [
        (
            ["telegram_probe", "/start"],
            {"tool": "telegram_probe", "delivered": True, "error": None},
            0,
            {"tool": "telegram_probe", "args": {"message": "/start"}},
        ),
        (
            ["telegram_click_button", "7", "ZGV0YWlscw=="],
            {"tool": "telegram_click_button", "delivered": True, "error": None},
            0,
            {
                "tool": "telegram_click_button",
                "args": {"message_id": 7, "callback_data": "ZGV0YWlscw=="},
            },
        ),
        (
            ["telegram_probe", "/start"],
            {
                "tool": "telegram_probe",
                "delivered": False,
                "error": "ValueError: The message cannot be empty",
            },
            1,
            {"tool": "telegram_probe", "args": {"message": "/start"}},
        ),
    ],
)
def test_telegram_cli_prints_the_capability_json_and_uses_error_value_for_exit_status(
    monkeypatch, argv, answer, expected_exit, expected_call
):
    body = json.dumps(answer)
    requests = []

    def urlopen(request, *, timeout):
        assert timeout == 180
        requests.append(request)
        return _Response(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("QA_CAPABILITY_URL", "http://qa.test/qa/call")
    monkeypatch.setenv("QA_CAPABILITY_TOKEN", "capability-token")
    monkeypatch.setattr(sys, "argv", ["qa", *argv])
    stdout = StringIO()

    with pytest.raises(SystemExit) as exited, redirect_stdout(stdout):
        exec(QA_PROBE_SCRIPT, {"__name__": "__main__"})  # noqa: S102 - injected script source

    assert exited.value.code == expected_exit
    assert stdout.getvalue() == body + "\n"
    assert len(requests) == 1
    assert json.loads(requests[0].data) == expected_call
