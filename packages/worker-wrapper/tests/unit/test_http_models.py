"""Tests for HTTP server request/response models."""

from pydantic import ValidationError
import pytest
from worker_wrapper.http_models import (
    ResultRequest,
    to_worker_result,
)

from shared.contracts.queues.worker_result import (
    WorkerBlockedResult,
    WorkerCompletedResult,
    WorkerResultStatus,
)


class TestResultRequestSuccess:
    def test_valid_success(self):
        req = ResultRequest(success=True, commit="abc123def", summary="Implemented feature X")
        assert req.success is True
        assert req.commit == "abc123def"
        assert req.summary == "Implemented feature X"

    def test_success_missing_commit(self):
        with pytest.raises(ValidationError, match="commit.*required when success=true"):
            ResultRequest(success=True, summary="done")

    def test_success_missing_summary(self):
        with pytest.raises(ValidationError, match="summary.*required when success=true"):
            ResultRequest(success=True, commit="abc123")

    def test_success_empty_commit_rejected(self):
        with pytest.raises(ValidationError):
            ResultRequest(success=True, commit="", summary="done")

    def test_success_empty_summary_rejected(self):
        with pytest.raises(ValidationError):
            ResultRequest(success=True, commit="abc123", summary="")

    def test_success_ignores_reason(self):
        req = ResultRequest(
            success=True, commit="abc123", summary="done", reason="should be ignored"
        )
        assert req.reason == "should be ignored"  # stored but not used


class TestResultRequestFailure:
    def test_valid_failure(self):
        req = ResultRequest(success=False, reason="Tests don't pass after 3 attempts")
        assert req.success is False
        assert req.reason == "Tests don't pass after 3 attempts"

    def test_failure_missing_reason(self):
        with pytest.raises(ValidationError, match="reason.*required when success=false"):
            ResultRequest(success=False)

    def test_failure_empty_reason_rejected(self):
        with pytest.raises(ValidationError):
            ResultRequest(success=False, reason="")

    def test_failure_whitespace_reason_rejected(self):
        with pytest.raises(ValidationError):
            ResultRequest(success=False, reason="   ")


class TestToWorkerResult:
    def test_success_result(self):
        req = ResultRequest(success=True, commit="abc123def", summary="Added login endpoint")
        result = to_worker_result(req)
        assert isinstance(result, WorkerCompletedResult)
        assert result.status == WorkerResultStatus.COMPLETED
        assert result.commit_sha == "abc123def"
        assert result.content == "Added login endpoint"

    def test_failure_result(self):
        req = ResultRequest(success=False, reason="Need API key for external service")
        result = to_worker_result(req)
        assert isinstance(result, WorkerBlockedResult)
        assert result.status == WorkerResultStatus.BLOCKED
        assert result.block_reason == "Need API key for external service"
