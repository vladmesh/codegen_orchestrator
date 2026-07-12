"""Unit tests for the typed worker result contract."""

import json

from pydantic import ValidationError
import pytest

from shared.contracts.queues.worker_result import (
    WorkerBlockedResult,
    WorkerCompletedResult,
    WorkerFailedResult,
    WorkerResultStatus,
    parse_worker_result,
)


class TestWorkerResultRoundTrip:
    """Producer serializes a model; consumer validates the same wire back."""

    def test_completed_round_trip(self):
        model = WorkerCompletedResult(
            commit_sha="abc123",
            content="Implemented feature",
            worker_report="REPORT",
            agent_stdout_tail="tail",
        )
        wire = json.loads(json.dumps(model.model_dump(mode="json")))
        parsed = parse_worker_result(wire)
        assert parsed == model
        assert isinstance(parsed, WorkerCompletedResult)
        assert parsed.status == WorkerResultStatus.COMPLETED

    def test_failed_round_trip(self):
        model = WorkerFailedResult(error="Agent crashed", agent_stdout_tail="boom")
        parsed = parse_worker_result(model.model_dump(mode="json"))
        assert parsed == model
        assert isinstance(parsed, WorkerFailedResult)

    def test_blocked_round_trip(self):
        model = WorkerBlockedResult(block_reason="Missing credentials")
        parsed = parse_worker_result(model.model_dump(mode="json"))
        assert parsed == model
        assert parsed.status == WorkerResultStatus.BLOCKED

    def test_rejected_shares_blocked_shape(self):
        parsed = parse_worker_result(
            {"status": "rejected", "block_reason": "REGISTRY_PASSWORD empty"}
        )
        assert isinstance(parsed, WorkerBlockedResult)
        assert parsed.status == WorkerResultStatus.REJECTED
        assert parsed.block_reason == "REGISTRY_PASSWORD empty"


class TestWorkerResultValidation:
    """Invalid payloads are rejected, not coerced."""

    def test_unknown_status_rejected(self):
        with pytest.raises(ValidationError):
            parse_worker_result({"status": "success", "content": "x"})

    def test_synonym_reason_key_rejected(self):
        # legacy reject_reason is no longer part of the wire
        with pytest.raises(ValidationError):
            parse_worker_result({"status": "rejected", "reject_reason": "x"})

    def test_completed_requires_commit_and_content(self):
        with pytest.raises(ValidationError):
            parse_worker_result({"status": "completed", "content": "no commit"})

    def test_extra_key_rejected(self):
        with pytest.raises(ValidationError):
            parse_worker_result(
                {
                    "status": "completed",
                    "commit_sha": "abc",
                    "content": "x",
                    "surprise": "field",
                }
            )

    def test_blocked_requires_reason(self):
        with pytest.raises(ValidationError):
            parse_worker_result({"status": "blocked"})
