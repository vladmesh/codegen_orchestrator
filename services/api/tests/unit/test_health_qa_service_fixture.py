"""The health QA service fixture publishes a valid unavailable diagnostic."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAvailability,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.vocab import AgentType


@pytest.mark.asyncio
async def test_health_qa_service_snapshot_round_trips_without_model_sessions():
    path = Path(__file__).resolve().parents[1] / "service/conftest.py"
    spec = importlib.util.spec_from_file_location("health_qa_api_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    redis = AsyncMock()
    snapshot = await module.unavailable_executor_snapshot.__wrapped__(redis, None)
    key, payload = redis.set.call_args.args
    assert key == EXECUTOR_DIAGNOSTICS_REDIS_KEY
    assert ExecutorDiagnosticSnapshot.model_validate_json(payload) == snapshot
    assert {item.executor for item in snapshot.diagnostics} == {AgentType.CLAUDE, AgentType.CODEX}
    assert all(
        item.availability == ExecutorAvailability.UNAVAILABLE
        and item.reason_code == "profile_logged_out"
        for item in snapshot.diagnostics
    )
