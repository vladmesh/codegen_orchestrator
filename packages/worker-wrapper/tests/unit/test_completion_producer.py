"""The installed completion command obtains its claim from Git, never from argv."""

import json
import subprocess
from unittest.mock import MagicMock

import pytest
from worker_wrapper import report_completed


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch):
    monkeypatch.setattr(report_completed, "setup_logging", MagicMock())


def test_producer_serializes_native_full_sha_and_agent_summary(monkeypatch):
    sha = "b0009d896fd15812333b5d084d2af76039f576df"
    git = MagicMock(return_value=subprocess.CompletedProcess([], 0, sha + "\n", ""))
    monkeypatch.setattr(report_completed.subprocess, "run", git)
    summary = 'quotes " backslash \\ newline\n Unicode Привет; $(false)'
    payload = report_completed.completed_payload(summary)
    assert json.loads(payload) == {"success": True, "commit": sha, "summary": summary}
    assert git.call_args.args[0][-3:] == ["rev-parse", "--verify", "HEAD^{commit}"]
    assert str(git.call_args.kwargs["cwd"]) == "/workspace"


def test_unreadable_git_never_submits_a_result(monkeypatch):
    monkeypatch.setattr(
        report_completed.subprocess,
        "run",
        MagicMock(side_effect=subprocess.CalledProcessError(128, ["git"])),
    )
    connection = MagicMock()
    monkeypatch.setattr(report_completed, "HTTPConnection", connection)
    monkeypatch.setattr("sys.argv", ["worker-report-completed", "--summary", "finished"])
    with pytest.raises(SystemExit) as exc:
        report_completed.main()
    assert exc.value.code == 1
    connection.assert_not_called()


@pytest.mark.parametrize("status,exit_code", [(200, 0), (400, 1), (409, 1)])
def test_command_preserves_http_refusal_evidence(monkeypatch, status, exit_code):
    monkeypatch.setattr(report_completed, "completed_payload", lambda summary: b"payload")
    connection = MagicMock()
    response = connection.return_value.getresponse.return_value
    response.status = status
    response.read.return_value = b'{"error":"object_missing"}'
    monkeypatch.setattr(report_completed, "HTTPConnection", connection)
    monkeypatch.setattr("sys.argv", ["worker-report-completed", "--summary", "finished"])
    with pytest.raises(SystemExit) as exc:
        report_completed.main()
    assert exc.value.code == exit_code
    connection.return_value.request.assert_called_once_with(
        "POST",
        "/result",
        body=b"payload",
        headers={"Content-Type": "application/json"},
    )
