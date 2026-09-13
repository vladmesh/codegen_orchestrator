"""Strict evidence for engineering work that did or did not reach an agent."""

from pydantic import ValidationError
import pytest

from shared.contracts.dto.engineering_execution import (
    ENGINEERING_INFRASTRUCTURE_KEY,
    EngineeringExecutionEvidence,
    EngineeringExecutionPhase,
    EngineeringInfrastructurePark,
    EngineeringInfrastructureRefusal,
)
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.worker_turn import AttemptTurnMetadata


def test_pre_agent_refusal_requires_a_typed_reason() -> None:
    with pytest.raises(ValidationError):
        EngineeringExecutionEvidence(execution_phase=EngineeringExecutionPhase.PRE_AGENT_REFUSED)


def test_agent_started_prohibits_a_refusal_reason() -> None:
    with pytest.raises(ValidationError):
        EngineeringExecutionEvidence(
            execution_phase=EngineeringExecutionPhase.AGENT_STARTED,
            infrastructure_refusal=EngineeringInfrastructureRefusal.PROJECT_LOCKED,
        )


def test_execution_evidence_round_trips_through_attempt_metadata_and_run_result() -> None:
    evidence = EngineeringExecutionEvidence(
        execution_phase=EngineeringExecutionPhase.PRE_AGENT_REFUSED,
        infrastructure_refusal=EngineeringInfrastructureRefusal.PROJECT_LOCKED,
    )

    metadata = AttemptTurnMetadata(execution=evidence).as_run_metadata()
    assert AttemptTurnMetadata.from_run_metadata(metadata).execution == evidence
    result = EngineeringRunResult(
        engineering_status="failed",
        execution=evidence,
    )
    assert result.execution == evidence


def test_infrastructure_park_is_exactly_keyed_to_task_attempt_and_reason() -> None:
    park = EngineeringInfrastructurePark(
        task_id="task-1",
        attempt_id="eng-1",
        refusal=EngineeringInfrastructureRefusal.EXECUTOR_UNAVAILABLE,
        detail="The selected executor has no usable profile.",
    )

    assert park.as_metadata() == {
        ENGINEERING_INFRASTRUCTURE_KEY: {
            "execution_phase": "pre_agent_refused",
            "refusal": "executor_unavailable",
            "task_id": "task-1",
            "attempt_id": "eng-1",
            "detail": "The selected executor has no usable profile.",
        }
    }
