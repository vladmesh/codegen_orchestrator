"""Contract proofs for immutable paid-run executor decisions."""

from pydantic import ValidationError
import pytest

from shared.contracts.dto.executor_decision import (
    EXECUTOR_DECISION_METADATA_KEY,
    ExecutorDecision,
    ExecutorDecisionSource,
)
from shared.contracts.dto.run import RunType
from shared.contracts.vocab import AgentType


def test_executor_decision_serializes_under_its_declared_metadata_key():
    decision = ExecutorDecision(
        attempt_kind=RunType.ENGINEERING,
        agent_type=AgentType.FACTORY,
        source=ExecutorDecisionSource.PROJECT_PIN,
        policy_version="v1",
        reason="Engineering executor pinned by project configuration.",
    )

    metadata = decision.as_run_metadata()

    assert metadata == {EXECUTOR_DECISION_METADATA_KEY: decision.model_dump(mode="json")}
    assert ExecutorDecision.from_run_metadata(metadata) == decision


def test_qa_decision_rejects_factory_even_when_the_agent_type_is_valid():
    with pytest.raises(ValidationError, match="QA executor"):
        ExecutorDecision(
            attempt_kind=RunType.QA,
            agent_type=AgentType.FACTORY,
            source=ExecutorDecisionSource.QA_API_SETTING,
            policy_version="v1",
            reason="QA executor selected by API QA_EXECUTOR_AGENT_TYPE.",
        )


def test_executor_decision_rejects_unexpected_persisted_members():
    with pytest.raises(ValidationError, match="unexpected"):
        ExecutorDecision.from_run_metadata(
            {
                EXECUTOR_DECISION_METADATA_KEY: {
                    "attempt_kind": "engineering",
                    "agent_type": "codex",
                    "source": "api_default",
                    "policy_version": "v2",
                    "reason": "configured default",
                    "unexpected": "must not be discarded",
                }
            }
        )
