"""Shared paid-run policy fixture for consumer tests unrelated to policy loading."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.run import RunType
from shared.contracts.vocab import AgentType
from shared.contracts.worker_turn import AttemptTurnMetadata


@pytest.fixture(autouse=True)
def _paid_run_executor_snapshot():
    """Keep legacy consumer tests focused on their own post-admission behavior."""
    with (
        patch(
            "src.consumers.engineering._load_engineering_executor_decision",
            new_callable=AsyncMock,
            return_value=ExecutorDecision(
                attempt_kind=RunType.ENGINEERING,
                agent_type=AgentType.CLAUDE,
                source=ExecutorDecisionSource.API_DEFAULT,
                policy_version="v1",
                reason="Engineering executor selected by API DEFAULT_AGENT_TYPE.",
            ),
        ),
        patch(
            "src.consumers.engineering._recorded_attempt_turn",
            new_callable=AsyncMock,
            return_value=AttemptTurnMetadata(),
        ),
    ):
        yield
