"""Unit test configuration."""

import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.config_store import ConfigStore
from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.run import RunType
from shared.contracts.vocab import AgentType
from shared.contracts.worker_turn import AttemptTurnMetadata

# Add /app to sys.path so that 'src' module can be imported.
# This is needed because the volume mount for tests doesn't include the src module.
app_path = Path("/app")
if app_path.exists() and str(app_path) not in sys.path:
    sys.path.insert(0, str(app_path))

# Provide required env vars for Settings validation in unit tests
os.environ.setdefault("API_BASE_URL", "http://api:8000")


@pytest.fixture(autouse=True)
def mock_deploy_config_store(monkeypatch):
    """Keep deploy-consumer unit tests independent of the system-config API."""
    from src.consumers import deploy

    store = MagicMock(spec=ConfigStore)
    store.get_int.return_value = 3600
    monkeypatch.setattr(deploy, "_config", store)
    return store


@pytest.fixture(autouse=True)
def paid_run_executor_for_legacy_unit_states(monkeypatch):
    """Keep pre-decision unit fixtures focused on their stated behavior."""
    from src.consumers import engineering
    from src.nodes.developer import DeveloperNode

    engineering_decision = ExecutorDecision(
        attempt_kind=RunType.ENGINEERING,
        agent_type=AgentType.CLAUDE,
        source=ExecutorDecisionSource.API_DEFAULT,
        policy_version="v1",
        reason="Engineering executor selected by API DEFAULT_AGENT_TYPE.",
    )
    original_run = DeveloperNode.run

    async def run_with_decision(self, state):
        state.setdefault("executor_decision", engineering_decision)
        return await original_run(self, state)

    monkeypatch.setattr(DeveloperNode, "run", run_with_decision)
    monkeypatch.setattr(
        engineering,
        "_load_engineering_executor_decision",
        AsyncMock(return_value=engineering_decision),
    )
    monkeypatch.setattr(
        engineering,
        "_recorded_attempt_turn",
        AsyncMock(return_value=AttemptTurnMetadata()),
    )
