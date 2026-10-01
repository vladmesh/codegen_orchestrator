"""Contract tests for the typed worker input turn."""

from pydantic import ValidationError
import pytest

from shared.contracts.worker_turn import WorkerTurnInput
from shared.queues import worker_input_stream, worker_output_stream


def test_engineering_turn_requires_attempt_and_deadline_together():
    with pytest.raises(ValidationError, match="provided together"):
        WorkerTurnInput(
            request_id="req-1",
            attempt_id="attempt-1",
            prompt="do the work",
        )


def test_qa_turn_can_omit_engineering_supervision_fields():
    turn = WorkerTurnInput(request_id="qa-1", prompt="test the deployment")
    assert turn.attempt_id is None
    assert turn.turn_deadline_seconds is None


def test_turn_rejects_unknown_fields_and_ambiguous_prompt():
    with pytest.raises(ValidationError):
        WorkerTurnInput.model_validate({"request_id": "req-1", "prompt": "one", "mystery": "two"})
    with pytest.raises(ValidationError, match="exactly one"):
        WorkerTurnInput(request_id="req-1", prompt="one", content="two")


def test_worker_stream_builders_share_one_spelling():
    assert worker_input_stream("dev-1") == "worker:dev-1:input"
    assert worker_output_stream("dev-1") == "worker:dev-1:output"
