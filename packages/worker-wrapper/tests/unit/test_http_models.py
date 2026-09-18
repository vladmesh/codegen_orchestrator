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


class TestFailedStepSurvivesTheBoundary:
    """The runner names the step it failed on; the pipeline has to hear it.

    Before this, `ResultRequest` modelled only success/commit/summary/reason, so
    `step`, `error_class` and `exit_code` were dropped by Pydantic at this exact
    boundary and a red run said only "noop runner step setup failed" in prose.
    """

    def test_failure_carries_the_step_into_the_blocked_reason(self):
        req = ResultRequest(
            success=False,
            reason="noop runner step setup failed",
            step="setup",
            error_class="SetupFailed",
            exit_code=2,
        )
        result = to_worker_result(req)
        assert isinstance(result, WorkerBlockedResult)
        assert result.block_reason == (
            "noop runner step setup failed (step=setup, error_class=SetupFailed, exit_code=2)"
        )

    def test_a_failure_without_step_facts_reads_exactly_as_before(self):
        req = ResultRequest(success=False, reason="Need API key for external service")
        assert to_worker_result(req).block_reason == "Need API key for external service"

    def test_partial_step_facts_are_reported_without_inventing_the_rest(self):
        req = ResultRequest(success=False, reason="agent gave up", step="push")
        assert to_worker_result(req).block_reason == "agent gave up (step=push)"

    def test_exit_code_zero_is_a_value_not_an_absence(self):
        req = ResultRequest(success=False, reason="agent gave up", exit_code=0)
        assert to_worker_result(req).block_reason == "agent gave up (exit_code=0)"

    def test_step_facts_are_refused_on_a_success(self):
        with pytest.raises(ValidationError):
            ResultRequest(success=True, commit="abc123", summary="done", step="setup")

    def test_empty_step_is_refused_rather_than_carried(self):
        with pytest.raises(ValidationError):
            ResultRequest(success=False, reason="failed", step="  ")
