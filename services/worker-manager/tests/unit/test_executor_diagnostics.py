"""Worker-manager publishes credential-safe executor availability."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.contracts.dto.executor_diagnostics import (
    ExecutorAuthMode,
    ExecutorAvailability,
    ExecutorProfileCondition,
)
from shared.contracts.vocab import AgentType
from shared.tests.executor_diagnostic_cases import host_profile
from src.executor_diagnostics import ExecutorDiagnostics
from src.host_profile import ProfileInspection
from src.manager import WorkerManager


def _inspection(condition: ExecutorProfileCondition) -> ProfileInspection:
    refusal = None if condition is ExecutorProfileCondition.HEALTHY else "synthetic refusal"
    return ProfileInspection(host_profile(condition), refusal)


@pytest.mark.asyncio
async def test_publish_executor_diagnostics_writes_a_bounded_snapshot(monkeypatch):
    redis = AsyncMock()
    redis.scan_iter = MagicMock()
    redis.scan_iter.return_value = _empty_scan()
    manager = WorkerManager(redis=redis, docker_client=MagicMock())

    await manager.publish_executor_diagnostics()

    redis.set.assert_awaited_once()
    assert redis.set.await_args.args[0] == "executor:diagnostics:v2"
    assert redis.set.await_args.kwargs["ex"] > 0


def test_claude_diagnostic_uses_manager_visible_validation_path(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.executor_diagnostics as diagnostics_module

    now = datetime.now(UTC)
    monkeypatch.setattr(
        diagnostics_module.settings, "HOST_CLAUDE_DIR", "/host-source/.claude", raising=False
    )
    monkeypatch.setattr(
        diagnostics_module.settings, "HOST_CLAUDE_VALIDATION_PATH", "/host-claude", raising=False
    )
    observed: list[str | None] = []

    def inspect(path, *, now):
        observed.append(path)
        return _inspection(ExecutorProfileCondition.HEALTHY)

    monkeypatch.setattr("src.claude_auth.inspect_claude_host_session", inspect)
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
    monkeypatch.setattr(
        diagnostics_module.settings, "HOST_CODEX_HOME", "/host-source/.codex", raising=False
    )
    # A usable profile, so only the inventory decides the unknown state.
    monkeypatch.setattr(
        "src.codex_auth.inspect_codex_host_session",
        lambda _path, *, now: _inspection(ExecutorProfileCondition.HEALTHY),
    )
    diagnostics = ExecutorDiagnostics(redis=AsyncMock(), docker=MagicMock())
    diagnostic = diagnostics._executor_diagnostic(
        AgentType.CODEX, now, now + timedelta(seconds=60), None
    )

    assert diagnostic.availability is ExecutorAvailability.UNKNOWN
    assert diagnostic.active_lease_count is None


def test_stand_token_diagnostic_accepts_manager_local_opaque_claude_metadata(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.executor_diagnostics as diagnostics_module

    now = datetime.now(UTC)
    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", "stand", raising=False)
    monkeypatch.setattr(
        diagnostics_module.settings,
        "STAND_CLAUDE_CODE_OAUTH_TOKEN",
        "sk-ant-oat01-fake",
        raising=False,
    )
    monkeypatch.setattr(
        diagnostics_module.settings,
        "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        (now + timedelta(hours=1)).isoformat(),
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


def test_stand_codex_diagnostic_refuses_an_invalid_refreshable_profile(monkeypatch):
    from datetime import UTC, datetime, timedelta

    import src.executor_diagnostics as diagnostics_module

    now = datetime.now(UTC)
    monkeypatch.setattr(diagnostics_module.settings, "LIVE_CONTOUR", "stand", raising=False)
    monkeypatch.setattr(
        diagnostics_module.settings, "HOST_CODEX_HOME", "/host/stand-codex", raising=False
    )
    monkeypatch.setattr(
        diagnostics_module.settings, "HOST_CODEX_VALIDATION_PATH", "/host-codex", raising=False
    )
    monkeypatch.setattr(
        "src.codex_auth.inspect_codex_host_session",
        lambda _profile, *, now: _inspection(ExecutorProfileCondition.UNUSABLE),
    )
    diagnostics = ExecutorDiagnostics(redis=AsyncMock(), docker=MagicMock())

    diagnostic = diagnostics._executor_diagnostic(
        AgentType.CODEX,
        now,
        now + timedelta(seconds=60),
        {AgentType.CLAUDE: 0, AgentType.CODEX: 0},
    )

    assert diagnostic.auth_mode is ExecutorAuthMode.HOST_SESSION
    assert diagnostic.availability is ExecutorAvailability.UNAVAILABLE
    assert diagnostic.reason_code == "local_auth_invalid"


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
    redis = _inventory_redis(
        ["workspace-lock-refusal"], statuses={"workspace-lock-refusal": "FAILED"}
    )
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
    docker.list_containers = AsyncMock(
        return_value=[_container("worker-1", "codex", "host_session", status="exited")]
    )

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
    docker.list_containers = AsyncMock(
        return_value=[_container("worker-1", "codex", "host_session")]
    )

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
    docker.list_containers = AsyncMock(
        return_value=[_container("worker-1", "codex", "host_session")]
    )

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "UNKNOWN"])
async def test_absent_or_unknown_status_makes_lease_inventory_unknown(status):
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": status})
    docker = MagicMock()
    docker.list_containers = AsyncMock(
        return_value=[_container("worker-1", "codex", "host_session")]
    )

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_unreadable_status_makes_lease_inventory_unknown():
    redis = _inventory_redis(["worker-1"])
    redis.hget.side_effect = RuntimeError("redis unavailable")
    docker = MagicMock()
    docker.list_containers = AsyncMock(
        return_value=[_container("worker-1", "codex", "host_session")]
    )

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_exited_container_with_running_redis_status_makes_inventory_unknown():
    redis = _inventory_redis(["worker-1"], statuses={"worker-1": "RUNNING"})
    docker = MagicMock()
    docker.list_containers = AsyncMock(
        return_value=[_container("worker-1", "codex", "host_session", status="exited")]
    )

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_label_disagreement_makes_lease_inventory_unknown():
    redis = _inventory_redis(["worker-1"])
    docker = MagicMock()
    docker.list_containers = AsyncMock(
        return_value=[_container("worker-1", "claude", "host_session")]
    )

    assert await ExecutorDiagnostics(redis=redis, docker=docker)._executor_leases() is None


@pytest.mark.asyncio
async def test_reconciler_returns_exact_mixed_executor_counts():
    redis = _inventory_redis(
        ["claude-1", "codex-1"], agent_types={"claude-1": "claude", "codex-1": "codex"}
    )
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
        "com.codegen.story.id": "story",
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
            "story_id": "story",
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


# --- one tick: publication plus alert reconciliation ---------------------------------


def _synthetic_profiles(tmp_path, monkeypatch, *, claude_credentials: dict):
    import json

    import src.executor_diagnostics as diagnostics_module

    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / ".credentials.json").write_text(json.dumps(claude_credentials))
    codex = tmp_path / "codex"
    codex.mkdir(mode=0o700)
    codex.chmod(0o700)
    (codex / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    # A synthetic unsigned JWT: header {"alg":"none"}, payload {}.
                    "id_token": "eyJhbGciOiJub25lIn0.e30.c2ln",
                    "access_token": "opaque-access",
                    "refresh_token": "opaque-refresh",
                },
            }
        )
    )
    (codex / "auth.json").chmod(0o600)
    (codex / ".codegen-codex.lock").touch()
    (codex / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
    (codex / "config.toml").chmod(0o600)
    settings = diagnostics_module.settings
    monkeypatch.setattr(settings, "LIVE_CONTOUR", None, raising=False)
    monkeypatch.setattr(settings, "HOST_CLAUDE_DIR", "/docker-host/.claude", raising=False)
    monkeypatch.setattr(settings, "HOST_CLAUDE_VALIDATION_PATH", str(claude), raising=False)
    monkeypatch.setattr(settings, "HOST_CODEX_HOME", "/docker-host/.codex", raising=False)
    monkeypatch.setattr(settings, "HOST_CODEX_VALIDATION_PATH", str(codex), raising=False)


class _Admins:
    def __init__(self):
        self.messages: list[str] = []

    async def __call__(self, message, level="info"):
        from shared.notifications import AdminDeliveryResult

        self.messages.append(message)
        return AdminDeliveryResult(configured=1, succeeded=1)


def _empty_docker():
    docker = MagicMock()
    docker.list_containers = AsyncMock(return_value=[])
    return docker


@pytest.mark.asyncio
async def test_each_tick_publishes_both_profiles_and_alerts_once(tmp_path, monkeypatch):
    import json

    from fakeredis import aioredis

    from shared.contracts.dto.executor_diagnostics import (
        EXECUTOR_DIAGNOSTICS_REDIS_KEY,
        ExecutorDiagnosticSnapshot,
    )
    from src.profile_alerts import ExecutorProfileAlerts

    _synthetic_profiles(tmp_path, monkeypatch, claude_credentials={"claudeAiOauth": {}})
    redis = aioredis.FakeRedis(decode_responses=True)
    admins = _Admins()
    diagnostics = ExecutorDiagnostics(
        redis=redis, docker=_empty_docker(), alerts=ExecutorProfileAlerts(redis, admins)
    )

    # Startup publication, then the periodic ticks.
    for _ in range(3):
        await diagnostics.publish()

    stored = ExecutorDiagnosticSnapshot.model_validate_json(
        await redis.get(EXECUTOR_DIAGNOSTICS_REDIS_KEY)
    )
    assert 0 < await redis.ttl(EXECUTOR_DIAGNOSTICS_REDIS_KEY) <= 90
    claude = stored.for_executor(AgentType.CLAUDE, stored.observed_at)
    codex = stored.for_executor(AgentType.CODEX, stored.observed_at)
    assert (claude.availability, claude.reason_code) == (
        ExecutorAvailability.UNAVAILABLE,
        "profile_logged_out",
    )
    assert claude.profile.condition is ExecutorProfileCondition.LOGGED_OUT
    assert codex.availability is ExecutorAvailability.AVAILABLE
    assert codex.profile.condition is ExecutorProfileCondition.HEALTHY
    assert admins.messages == [
        "Claude executor host-session profile needs attention: Host-session profile is logged out."
    ]
    raw = await redis.get(EXECUTOR_DIAGNOSTICS_REDIS_KEY)
    assert "opaque-access" not in raw and str(tmp_path) not in raw and "docker-host" not in raw
    assert json.loads(raw)["schema_version"] == "v2"
    await redis.aclose()


@pytest.mark.asyncio
async def test_a_failed_publication_still_reconciles_alerts_and_raises(tmp_path, monkeypatch):
    _synthetic_profiles(tmp_path, monkeypatch, claude_credentials={"claudeAiOauth": {}})
    redis = AsyncMock()
    redis.scan_iter = MagicMock(return_value=_empty_scan())
    redis.set.side_effect = ConnectionError("redis down")
    alerts = MagicMock()
    alerts.reconcile = AsyncMock()

    with pytest.raises(ConnectionError):
        await ExecutorDiagnostics(redis=redis, docker=_empty_docker(), alerts=alerts).publish()

    snapshot = alerts.reconcile.await_args.args[0]
    assert snapshot.for_executor(AgentType.CLAUDE, snapshot.observed_at).profile is not None


@pytest.mark.asyncio
async def test_alert_delivery_failure_never_suppresses_the_published_state(tmp_path, monkeypatch):
    from fakeredis import aioredis

    from shared.contracts.dto.executor_diagnostics import (
        EXECUTOR_DIAGNOSTICS_REDIS_KEY,
        ExecutorDiagnosticSnapshot,
    )
    from src.profile_alerts import ExecutorProfileAlerts

    _synthetic_profiles(tmp_path, monkeypatch, claude_credentials={"claudeAiOauth": {}})
    redis = aioredis.FakeRedis(decode_responses=True)

    async def broken_admins(message, level="info"):
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    await ExecutorDiagnostics(
        redis=redis, docker=_empty_docker(), alerts=ExecutorProfileAlerts(redis, broken_admins)
    ).publish()

    stored = ExecutorDiagnosticSnapshot.model_validate_json(
        await redis.get(EXECUTOR_DIAGNOSTICS_REDIS_KEY)
    )
    claude = stored.for_executor(AgentType.CLAUDE, stored.observed_at)
    assert claude.availability is ExecutorAvailability.UNAVAILABLE
    await redis.aclose()


@pytest.mark.asyncio
async def test_periodic_loop_keeps_publishing_after_a_failed_tick():
    import asyncio

    from src.main import run_periodic_task

    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("redis down")
        if calls == 3:
            raise asyncio.CancelledError

    await asyncio.wait_for(run_periodic_task(tick, interval=0, name="executor_diagnostics"), 2)

    assert calls == 3


def test_startup_publishes_before_serving_and_schedules_the_configured_interval():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
    startup = source.index("await worker_manager.publish_executor_diagnostics()")
    assert startup < source.index("yield")
    assert "interval=settings.EXECUTOR_DIAGNOSTICS_INTERVAL_SECONDS" in source
