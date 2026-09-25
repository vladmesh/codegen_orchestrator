"""The central executor's serialized QA capability command."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import sys
import urllib.error
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

    def close(self) -> None:
        return None


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


def test_probe_runs_a_python_script_and_records_its_evidence(monkeypatch, tmp_path):
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\nprint('probe stdout')\nprint('probe stderr', file=sys.stderr)\n"
        "raise SystemExit(7)\n"
    )
    calls = []

    def urlopen(request, *, timeout):
        calls.append(json.loads(request.data))
        return _Response(json.dumps({"tool": "record_probe", "id": "probe-1"}))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("QA_CAPABILITY_URL", "http://qa.test/qa/call")
    monkeypatch.setenv("QA_CAPABILITY_TOKEN", "capability-token")
    monkeypatch.setattr(sys, "argv", ["qa", "probe", "http", "health", str(script), "one"])
    stdout = StringIO()

    with pytest.raises(SystemExit) as exited, redirect_stdout(stdout):
        # stderr remains inherited so the injected CLI can print probe stderr.
        exec(QA_PROBE_SCRIPT, {"__name__": "__main__"})  # noqa: S102 - injected script source

    assert exited.value.code == 7
    assert stdout.getvalue().startswith("probe-1\nprobe stdout\n")
    [call] = calls
    assert call["tool"] == "record_probe"
    assert call["args"]["platform"] == "http"
    assert call["args"]["arguments"] == ["one"]
    assert call["args"]["exit_status"] == 7


@pytest.mark.parametrize(
    ("label", "suffix", "body", "expected_status"),
    [
        ("timeout", ".sh", "printf timed; sleep 1", 124),
        ("nonutf8", ".sh", "printf '\\377\\376 binary\\n'", 0),
        ("large", ".py", "print('x' * 1100000)", 0),
        ("empty", ".py", "pass", 0),
        ("nonzero", ".sh", "exit 7", 7),
        ("endpoint-413", ".py", "print('kept')", 0),
    ],
)
def test_probe_capture_is_total_for_hostile_script_and_endpoint_cases(
    monkeypatch, tmp_path, label, suffix, body, expected_status
):
    script = tmp_path / f"{label}{suffix}"
    script.write_text(body)
    requests = []

    def urlopen(request, *, timeout):
        requests.append(json.loads(request.data))
        if label == "endpoint-413":
            raise urllib.error.HTTPError(
                request.full_url, 413, "too large", {}, _Response("plain text")
            )
        return _Response(json.dumps({"tool": "record_probe", "id": "probe-1"}))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("QA_CAPABILITY_URL", "http://qa.test/qa/call")
    monkeypatch.setenv("QA_CAPABILITY_TOKEN", "capability-token")
    monkeypatch.setattr(sys, "argv", ["qa", "probe", "http", label, str(script)])
    stdout, stderr = StringIO(), StringIO()
    source = QA_PROBE_SCRIPT.replace("PROBE_TIMEOUT = 60", "PROBE_TIMEOUT = 0.5")

    with pytest.raises(SystemExit) as exited, redirect_stdout(stdout), redirect_stderr(stderr):
        exec(source, {"__name__": "__main__"})  # noqa: S102 - injected script source

    assert exited.value.code == expected_status
    assert len(requests) == 1
    record = requests[0]["args"]
    assert record["source"]
    assert len(record["stdout"]) <= 19000
    assert len(record["stderr"]) <= 19000
    if label == "nonutf8":
        assert "�" in record["stdout"]
    if label == "large":
        assert record["stdout_truncated"] is True
    if label == "endpoint-413":
        assert "record not retained" in stdout.getvalue()
