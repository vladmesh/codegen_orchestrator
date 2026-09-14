"""QA consumes the strict terminal WorkerResult rather than an untyped stream dict."""

from unittest.mock import MagicMock

import pytest

from src.clients.qa_worker import QAExecutorUnavailable, _terminal_result_of


def _finished(value):
    task = MagicMock()
    task.done.return_value = True
    task.cancelled.return_value = False
    task.result.return_value = value
    return task


def test_terminal_result_keeps_owned_locator() -> None:
    result = _terminal_result_of(
        _finished(
            {
                "status": "failed",
                "error": "agent exited before REPORT.md",
                "transcript_path": "v1/qa-worker-1/request-1.log",
                "transcript_truncated": False,
            }
        ),
        worker_id="qa-worker-1",
        request_id="request-1",
    )

    assert result is not None
    assert result.transcript_path == "v1/qa-worker-1/request-1.log"


def test_terminal_result_rejects_neighbour_locator() -> None:
    with pytest.raises(QAExecutorUnavailable, match="unavailable transcript evidence"):
        _terminal_result_of(
            _finished(
                {
                    "status": "failed",
                    "error": "agent exited",
                    "transcript_path": "v1/qa-neighbour/request-1.log",
                    "transcript_truncated": False,
                }
            ),
            worker_id="qa-worker-1",
            request_id="request-1",
        )


def test_agent_started_result_requires_locator_or_typed_save_failure() -> None:
    with pytest.raises(QAExecutorUnavailable, match="unavailable transcript evidence"):
        _terminal_result_of(
            _finished({"status": "failed", "error": "agent exited"}),
            worker_id="qa-worker-1",
            request_id="request-1",
        )

    result = _terminal_result_of(
        _finished(
            {
                "status": "failed",
                "error": "agent exited",
                "transcript_unavailable_reason": "save_failed",
            }
        ),
        worker_id="qa-worker-1",
        request_id="request-1",
    )
    assert result is not None
    assert result.transcript_unavailable_reason == "save_failed"
