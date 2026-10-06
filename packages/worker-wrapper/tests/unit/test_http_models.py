"""Tests for HTTP server request/response models."""

from pydantic import ValidationError
import pytest
from worker_wrapper.http_models import (
    MAX_STDERR_TAIL,
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


class TestFailedStepOutputSurvivesTheBoundary:
    """The failed step's stderr reaches the blocked reason, so failure_metadata keeps it.

    Mega-noop 37380025509: the commit hook refused (`make format` → ruff E402),
    the runner POSTed that stderr, and Pydantic dropped it here. The task's
    failure_metadata said only `CommitFailed, exit_code=1`, and the container
    holding the stdout copy was removed before the evidence collector ran.
    """

    HOOK_STDERR = (
        "[pre-commit] Running make format...\n"
        ".venv/bin/ruff check --fix .\n"
        "E402 Module level import not at top of file\n"
        "  --> services/backend/src/app/api/router.py:17:1\n"
        "make: *** [Makefile:101: format] Error 1\n"
    )

    def test_a_commit_hook_refusal_is_readable_from_the_blocked_reason(self):
        req = ResultRequest(
            success=False,
            reason="noop runner step commit failed",
            step="commit",
            error_class="CommitFailed",
            exit_code=1,
            stderr=self.HOOK_STDERR,
        )
        reason = to_worker_result(req).block_reason
        assert reason.startswith(
            "noop runner step commit failed (step=commit, error_class=CommitFailed, exit_code=1)"
        )
        assert "E402 Module level import not at top of file" in reason
        assert "services/backend/src/app/api/router.py:17:1" in reason

    def test_only_the_tail_of_a_long_output_is_kept(self):
        req = ResultRequest(
            success=False, reason="failed", step="setup", stderr="x" * 50_000 + "LAST LINE"
        )
        reason = to_worker_result(req).block_reason
        assert reason.endswith("LAST LINE")
        assert len(reason) < MAX_STDERR_TAIL + 200

    def test_credentials_in_the_output_are_redacted(self):
        req = ResultRequest(
            success=False,
            reason="failed",
            step="push",
            stderr="fatal: https://x-access-token:ghs_secretvalue@github.com/o/r.git denied",
        )
        assert "ghs_secretvalue" not in to_worker_result(req).block_reason

    def test_blank_output_adds_nothing(self):
        req = ResultRequest(success=False, reason="failed", step="push", stderr="  \n")
        assert to_worker_result(req).block_reason == "failed (step=push)"

    def test_output_is_refused_on_a_success(self):
        with pytest.raises(ValidationError):
            ResultRequest(success=True, commit="abc123", summary="done", stderr="boom")
