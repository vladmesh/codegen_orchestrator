"""Worker-manager publishes credential-safe executor availability."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from shared.contracts.dto.executor_diagnostics import ExecutorAvailability
from shared.contracts.vocab import AgentType
from src.manager import WorkerManager


@pytest.mark.asyncio
async def test_publish_executor_diagnostics_writes_a_bounded_snapshot(monkeypatch):
    redis = AsyncMock()
    redis.scan_iter = MagicMock()
    redis.scan_iter.return_value = _empty_scan()
    manager = WorkerManager(redis=redis, docker_client=MagicMock())

    await manager.publish_executor_diagnostics()

    redis.set.assert_awaited_once()
    assert redis.set.await_args.args[0] == "executor:diagnostics:v1"
    assert redis.set.await_args.kwargs["ex"] > 0


def test_claude_diagnostic_uses_manager_visible_validation_path(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.manager as manager_module

    now = datetime.now(UTC)
    monkeypatch.setattr(manager_module.settings, "HOST_CLAUDE_DIR", "/host-source/.claude", raising=False)
    monkeypatch.setattr(manager_module.settings, "HOST_CLAUDE_VALIDATION_PATH", "/host-claude", raising=False)
    observed: list[str | None] = []
    monkeypatch.setattr("src.claude_auth.validate_claude_host_session", lambda path: observed.append(path))
    manager = WorkerManager(redis=AsyncMock(), docker_client=MagicMock())

    diagnostic = manager._executor_diagnostic(
        AgentType.CLAUDE,
        now,
        now + timedelta(seconds=60),
        {AgentType.CLAUDE: 0, AgentType.CODEX: 0},
    )

    assert diagnostic.availability is ExecutorAvailability.AVAILABLE
    assert observed == ["/host-claude"]


def test_unreconciled_inventory_does_not_claim_zero_leases(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.manager as manager_module

    now = datetime.now(UTC)
    monkeypatch.setattr(manager_module.settings, "HOST_CODEX_HOME", "/host-source/.codex", raising=False)
    manager = WorkerManager(redis=AsyncMock(), docker_client=MagicMock())
    diagnostic = manager._executor_diagnostic(AgentType.CODEX, now, now + timedelta(seconds=60), None)

    assert diagnostic.availability is ExecutorAvailability.UNKNOWN
    assert diagnostic.active_lease_count is None


@pytest.mark.asyncio
async def test_redis_docker_disagreement_makes_lease_inventory_unknown():
    redis = AsyncMock()
    redis.scan_iter = MagicMock(return_value=_one_worker_scan())
    redis.hgetall.return_value = {"agent_type": "codex", "auth_mode": "host_session"}
    redis.hget.return_value = "running"
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[])
    manager = WorkerManager(redis=redis, docker_client=docker)

    assert await manager._executor_leases() is None


async def _empty_scan():
    if False:
        yield ""


async def _one_worker_scan():
    yield "worker:meta:worker-1"
