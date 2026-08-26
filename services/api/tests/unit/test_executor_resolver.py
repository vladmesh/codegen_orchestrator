"""The paid-run executor resolver preserves supported executor choices."""

from types import SimpleNamespace

import pytest

from shared.contracts.dto.executor_decision import ExecutorDecisionSource
from shared.contracts.dto.run import RunType
from shared.contracts.vocab import AgentType
from src.executor_resolver import ExecutorResolutionError, resolve_executor_decision


@pytest.mark.parametrize("agent_type", [AgentType.CLAUDE, AgentType.CODEX, AgentType.FACTORY])
def test_engineering_project_pin_is_the_decision(agent_type):
    decision = resolve_executor_decision(
        RunType.ENGINEERING,
        {"agent_type": agent_type.value},
        SimpleNamespace(
            default_agent_type=AgentType.CODEX, qa_executor_agent_type=AgentType.CLAUDE
        ),
    )

    assert decision.agent_type is agent_type
    assert decision.source is ExecutorDecisionSource.PROJECT_PIN


def test_engineering_without_a_pin_uses_the_api_default():
    decision = resolve_executor_decision(
        RunType.ENGINEERING,
        {},
        SimpleNamespace(
            default_agent_type=AgentType.CODEX, qa_executor_agent_type=AgentType.CLAUDE
        ),
    )

    assert decision.agent_type is AgentType.CODEX
    assert decision.source is ExecutorDecisionSource.API_DEFAULT


@pytest.mark.parametrize("agent_type", [AgentType.CLAUDE, AgentType.CODEX])
def test_qa_uses_the_api_qa_setting(agent_type):
    decision = resolve_executor_decision(
        RunType.QA,
        {},
        SimpleNamespace(default_agent_type=AgentType.FACTORY, qa_executor_agent_type=agent_type),
    )

    assert decision.agent_type is agent_type
    assert decision.source is ExecutorDecisionSource.QA_API_SETTING


def test_qa_rejects_factory_before_a_run_can_be_created():
    with pytest.raises(ExecutorResolutionError, match="QA_EXECUTOR_AGENT_TYPE"):
        resolve_executor_decision(
            RunType.QA,
            {},
            SimpleNamespace(
                default_agent_type=AgentType.CLAUDE, qa_executor_agent_type=AgentType.FACTORY
            ),
        )
