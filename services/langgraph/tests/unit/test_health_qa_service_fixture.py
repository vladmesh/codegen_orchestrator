"""Validate the service fixture payload without running its service boundaries."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAvailability,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.vocab import AgentType


@pytest.mark.asyncio
async def test_health_qa_service_snapshot_round_trips_without_model_sessions(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "service/test_health_qa_admission.py"
    spec = importlib.util.spec_from_file_location("health_qa_langgraph_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("TEST_API_BASE_URL", "http://fixture.invalid")
    monkeypatch.setattr(module, "LanggraphAPIClient", Mock())
    monkeypatch.setattr(module, "RedisStreamClient", Mock(return_value=AsyncMock()))

    class SnapshotPublished(Exception):
        pass

    # Stop at the fixture's first write, before any service setup or state transition.
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.side_effect = SnapshotPublished
    fixture = module.health_qa.__wrapped__(redis)
    with pytest.raises(SnapshotPublished):
        await anext(fixture)
    key, payload = redis.set.call_args.args
    assert key == EXECUTOR_DIAGNOSTICS_REDIS_KEY
    snapshot = ExecutorDiagnosticSnapshot.model_validate_json(payload)
    assert {item.executor for item in snapshot.diagnostics} == {AgentType.CLAUDE, AgentType.CODEX}
    assert all(
        item.availability == ExecutorAvailability.UNAVAILABLE
        and item.reason_code == "profile_logged_out"
        for item in snapshot.diagnostics
    )
