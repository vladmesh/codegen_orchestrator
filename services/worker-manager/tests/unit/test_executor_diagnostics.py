"""Worker-manager publishes credential-safe executor availability."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from shared.contracts.dto.executor_diagnostics import ExecutorAuthMode, ExecutorAvailability
from shared.contracts.vocab import AgentType
from src.executor_diagnostics import ExecutorDiagnostics
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

    import src.executor_diagnostics as diagnostics_module

    now = datetime.now(UTC)
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CLAUDE_DIR", "/host-source/.claude", raising=False)
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CLAUDE_VALIDATION_PATH", "/host-claude", raising=False)
    observed: list[str | None] = []
    monkeypatch.setattr("src.claude_auth.validate_claude_host_session", lambda path: observed.append(path))
    diagnostics = ExecutorDiagnostics(redis=AsyncMock(), docker=MagicMock())

    diagnostic = diagnostics._executor_diagnostic(
        AgentType.CLAUDE,
        now,
        now + timedelta(seconds=60),
        {AgentType.CLAUDE: 0, AgentType.CODEX: 0},
    )

    assert diagnostic.availability is ExecutorAvailability.AVAILABLE
    assert observed == ["/host-claude"]


def test_unreconciled_inventory_does_not_claim_zero_leases(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.executor_diagnostics as diagnostics_module

    now = datetime.now(UTC)
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_HOME", "/host-source/.codex", raising=False)
    diagnostics = ExecutorDiagnostics(redis=AsyncMock(), docker=MagicMock())
    diagnostic = diagnostics._executor_diagnostic(AgentType.CODEX, now, now + timedelta(seconds=60), None)

    assert diagnostic.availability is ExecutorAvailability.UNKNOWN
    assert diagnostic.active_lease_count is None


def test_stand_token_diagnostic_accepts_manager_local_opaque_claude_metadata(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.executor_diagnostics as diagnostics_module

    now = datetime.now(UTC)
    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", "stand", raising=False)
    monkeypatch.setattr(
        diagnostics_module.settings, "STAND_CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake", raising=False
    )
    monkeypatch.setattr(
        diagnostics_module.settings,
        "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        (now + timedelta(hours=1)).isoformat(),
        raising=False,
    )
    monkeypatch.setattr(
        diagnostics_module.settings,
        "STAND_CODEX_ACCESS_TOKEN",
        "header.eyJleHAiOjQxMDI0NDQ4MDB9.signature",
        raising=False,
    )
    diagnostics = ExecutorDiagnostics(redis=AsyncMock(), docker=MagicMock())

    diagnostic = diagnostics._executor_diagnostic(
        AgentType.CLAUDE,
        now,
        now + timedelta(seconds=60),
        {AgentType.CLAUDE: 0, AgentType.CODEX: 0},
    )

    assert diagnostic.auth_mode is ExecutorAuthMode.STAND_TOKEN
    assert diagnostic.availability is ExecutorAvailability.AVAILABLE
    assert diagnostic.reason_code == "stand_token_ready"


def test_stand_token_diagnostic_refuses_invalid_local_token_without_exposing_it(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.executor_diagnostics as diagnostics_module

    now = datetime.now(UTC)
    token = "fake-secret-codex-token"
    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", "stand", raising=False)
    monkeypatch.setattr(diagnostics_module.settings, "STAND_CODEX_ACCESS_TOKEN", token, raising=False)
    diagnostics = ExecutorDiagnostics(redis=AsyncMock(), docker=MagicMock())

    diagnostic = diagnostics._executor_diagnostic(
        AgentType.CODEX,
        now,
        now + timedelta(seconds=60),
        {AgentType.CLAUDE: 0, AgentType.CODEX: 0},
    )

    assert diagnostic.auth_mode is ExecutorAuthMode.STAND_TOKEN
    assert diagnostic.availability is ExecutorAvailability.UNAVAILABLE
    assert diagnostic.reason_code == "stand_token_invalid"
    assert token not in diagnostic.reason


@pytest.mark.asyncio
async def test_redis_docker_disagreement_makes_lease_inventory_unknown():
    redis = AsyncMock()
    redis.scan_iter = MagicMock(return_value=_one_worker_scan())
    redis.hgetall.return_value = {"agent_type": "codex", "auth_mode": "host_session"}
    redis.hget.return_value = "running"
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[])
    diagnostics = ExecutorDiagnostics(redis=redis, docker=docker)

    assert await diagnostics._executor_leases() is None


@pytest.mark.asyncio
async def test_terminal_redis_worker_without_a_container_is_a_settled_zero_lease():
    """A pre-container workspace-lock refusal retains FAILED metadata by design."""
    redis = _inventory_redis(["workspace-lock-refusal"], statuses={"workspace-lock-refusal": "FAILED"})
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() == {
        AgentType.CLAUDE: 0,
        AgentType.CODEX: 0,
    }


@pytest.mark.asyncio
async def test_terminal_redis_worker_with_a_terminal_matching_container_is_zero_lease():
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": "FAILED"})
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session", status="exited")])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() == {
        AgentType.CLAUDE: 0,
        AgentType.CODEX: 0,
    }


@pytest.mark.asyncio
async def test_nonterminal_redis_worker_without_a_container_remains_unknown():
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": "RUNNING"})
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_terminal_redis_worker_with_a_nonterminal_container_remains_unknown():
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": "FAILED"})
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session")])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_unknown_docker_state_makes_lease_inventory_unknown():
    redis = _inventory_redis(["worker-1"])
    docker = MagicMock()
    docker.list_containers = AsyncMock(
        return_value=[_container("worker-1", "codex", "host_session", status="removing")]
    )

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_duplicate_docker_identity_makes_lease_inventory_unknown():
    redis = _inventory_redis(["worker-1"])
    docker = MagicMock()
    docker.list_containers = AsyncMock(
        return_value=[
            _container("worker-1", "codex", "host_session"),
            _container("worker-1", "codex", "host_session"),
        ]
    )

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_docker_only_worker_makes_lease_inventory_unknown():
    redis = _inventory_redis([])
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session")])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "UNKNOWN"])
async def test_absent_or_unknown_status_makes_lease_inventory_unknown(status):
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": status})
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session")])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_unreadable_status_makes_lease_inventory_unknown():
    redis = _inventory_redis(["worker-1"])
    redis.hget.side_effect = RuntimeError("redis unavailable")
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session")])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_exited_container_with_running_redis_status_makes_inventory_unknown():
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": "RUNNING"})
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "codex", "host_session", status="exited")])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_label_disagreement_makes_lease_inventory_unknown():
    redis = _inventory_redis(["worker-1"])
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[_container("worker-1", "claude", "host_session")])

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


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

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() == {
        AgentType.CLAUDE: 1,
        AgentType.CODEX: 1,
    }


def test_disabled_executor_preserves_reconciled_live_lease_count(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.executor_diagnostics as diagnostics_module

    now = datetime.now(UTC)
    monkeypatch.setattr(diagnostics_module.settings, "HOST_CODEX_HOME", "", raising=False)
    diagnostic = ExecutorDiagnostics(redis=AsyncMock(), docker=MagicMock())._executor_diagnostic(
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
