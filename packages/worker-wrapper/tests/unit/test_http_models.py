"""Tests for HTTP server request/response models."""

from pydantic import ValidationError
import pytest
from worker_wrapper.http_models import (
    BlockerRequest,
    CompleteRequest,
    FailedRequest,
    to_redis_output,
)


class TestCompleteRequest:
    def test_valid(self):
        req = CompleteRequest(commit="abc123def", summary="Implemented feature X")
        assert req.commit == "abc123def"
        assert req.summary == "Implemented feature X"

    def test_missing_commit(self):
        with pytest.raises(ValidationError):
            CompleteRequest(summary="done")

    def test_missing_summary(self):
        with pytest.raises(ValidationError):
            CompleteRequest(commit="abc123")

    def test_empty_commit_rejected(self):
        with pytest.raises(ValidationError):
            CompleteRequest(commit="", summary="done")

    def test_empty_summary_rejected(self):
        with pytest.raises(ValidationError):
            CompleteRequest(commit="abc123", summary="")


class TestFailedRequest:
    def test_valid(self):
        req = FailedRequest(reason="Tests don't pass after 3 attempts")
        assert req.reason == "Tests don't pass after 3 attempts"

    def test_missing_reason(self):
        with pytest.raises(ValidationError):
            FailedRequest()

    def test_empty_reason_rejected(self):
        with pytest.raises(ValidationError):
            FailedRequest(reason="")


class TestBlockerRequest:
    def test_valid(self):
        req = BlockerRequest(reason="Spec ambiguous, need clarification on auth flow")
        assert req.reason == "Spec ambiguous, need clarification on auth flow"

    def test_missing_reason(self):
        with pytest.raises(ValidationError):
            BlockerRequest()

    def test_empty_reason_rejected(self):
        with pytest.raises(ValidationError):
            BlockerRequest(reason="")


class TestToRedisOutput:
    def test_complete(self):
        req = CompleteRequest(commit="abc123def", summary="Added login endpoint")
        result = to_redis_output("complete", req)
        assert result == {
            "status": "completed",
            "commit_sha": "abc123def",
            "content": "Added login endpoint",
        }

    def test_failed(self):
        req = FailedRequest(reason="Timeout on CI")
        result = to_redis_output("failed", req)
        assert result == {
            "status": "failed",
            "error": "Timeout on CI",
        }

    def test_blocker(self):
        req = BlockerRequest(reason="Need API key for external service")
        result = to_redis_output("blocker", req)
        assert result == {
            "status": "blocked",
            "block_reason": "Need API key for external service",
        }

    def test_unknown_action_raises(self):
        req = CompleteRequest(commit="abc", summary="done")
        with pytest.raises(ValueError, match="Unknown action"):
            to_redis_output("unknown", req)
