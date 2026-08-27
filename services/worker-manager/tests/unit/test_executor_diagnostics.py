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


@pytest.mark.asyncio
async def test_docker_only_worker_makes_lease_inventory_unknown():
    redis = _inventory_redis([])
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session")])

    assert await WorkerManager(redis=redis, docker_client=docker)._executor_leases() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "UNKNOWN"])
async def test_absent_or_unknown_status_makes_lease_inventory_unknown(status):
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": status})
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session")])

    assert await WorkerManager(redis=redis, docker_client=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_unreadable_status_makes_lease_inventory_unknown():
    redis = _inventory_redis(["worker-1"])
    redis.hget.side_effect = RuntimeError("redis unavailable")
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session")])

    assert await WorkerManager(redis=redis, docker_client=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_exited_container_with_running_redis_status_makes_inventory_unknown():
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": "RUNNING"})
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session", status="exited")])

    assert await WorkerManager(redis=redis, docker_client=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_label_disagreement_makes_lease_inventory_unknown():
    redis = _inventory_redis(["worker-1"])
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "claude", "host_session")])

    assert await WorkerManager(redis=redis, docker_client=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_reconciler_returns_exact_mixed_executor_counts():
    redis = _inventory_redis(["claude-1", "codex-1"], agent_types={"claude-1": "claude", "codex-1": "codex"})
    docker = MagicMock()
    docker.list_containers = AsyncMock(
        return_value=[
            _container("claude-1", "claude", "host_session"),
            _container("codex-1", "codex", "host_session"),
        ]
    )

    assert await WorkerManager(redis=redis, docker_client=docker)._executor_leases() == {
        AgentType.CLAUDE: 1,
        AgentType.CODEX: 1,
    }


def test_disabled_executor_preserves_reconciled_live_lease_count(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.manager as manager_module

    now = datetime.now(UTC)
    monkeypatch.setattr(manager_module.settings, "HOST_CODEX_HOME", "", raising=False)
    diagnostic = WorkerManager(redis=AsyncMock(), docker_client=MagicMock())._executor_diagnostic(
        AgentType.CODEX,
        now,
        now + timedelta(seconds=60),
        {AgentType.CLAUDE: 0, AgentType.CODEX: 2},
    )

    assert diagnostic.availability is ExecutorAvailability.UNAVAILABLE
    assert diagnostic.active_lease_count == 2


async def _empty_scan():
    if False:
        yield ""


async def _one_worker_scan():
    yield "worker:meta:worker-1"


def _container(worker_id: str, agent_type: str, auth_mode: str, *, status: str = "running"):
    container = MagicMock()
    container.labels = {
        "com.codegen.worker.id": worker_id,
        "com.codegen.project.id": "project",
        "com.codegen.run.id": "run",
        "com.codegen.attempt.id": "attempt",
        "com.codegen.agent_type": agent_type,
        "com.codegen.auth_mode": auth_mode,
    }
    container.status = status
    return container


def _inventory_redis(worker_ids, *, statuses=None, agent_types=None):
    statuses = statuses or {}
    agent_types = agent_types or {}
    redis = AsyncMock()

    async def scan(**_kwargs):
        for worker_id in worker_ids:
            yield f"worker:meta:{worker_id}"

    async def hgetall(key):
        worker_id = str(key).rsplit(":", 1)[-1]
        return {
            "project_id": "project",
            "run_id": "run",
            "attempt_id": "attempt",
            "agent_type": agent_types.get(worker_id, "codex"),
            "auth_mode": "host_session",
        }

    async def hget(key, _field):
        worker_id = str(key).rsplit(":", 1)[-1]
        return statuses.get(worker_id, "RUNNING")

    redis.scan_iter = scan
    redis.hgetall.side_effect = hgetall
    redis.hget.side_effect = hget
    return redis
