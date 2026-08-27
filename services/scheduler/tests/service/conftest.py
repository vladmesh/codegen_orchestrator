from datetime import UTC, datetime, timedelta
import os
from unittest.mock import patch

import pytest
from redis.asyncio import Redis
import respx

from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAuthMode,
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.vocab import AgentType
from shared.tests.mocks.github import MockGitHubClient


@pytest.fixture
def mock_github():
    """Universal GitHub Mock for service tests."""
    mock = MockGitHubClient()
    # Patcher to replace the real client in src.tasks.github_sync
    with patch("shared.clients.github.GitHubAppClient", return_value=mock):
        yield mock


@pytest.fixture
def time4vps_mock():
    """Respx mock for Time4VPS API."""
    with respx.mock(base_url="https://billing.time4vps.com", assert_all_called=False) as respx_mock:
        # Allow requests to the internal API service to pass through
        respx_mock.route(host="api").pass_through()
        yield respx_mock


@pytest.fixture
async def api_client():
    """Real SchedulerAPIClient configured from env."""
    from src.clients.api import api_client as client

    now = datetime.now(UTC)
    expiry = now + timedelta(hours=1)
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        await redis.set(
            EXECUTOR_DIAGNOSTICS_REDIS_KEY,
            ExecutorDiagnosticSnapshot(
                version="scheduler-service-test-diagnostics",
                observed_at=now,
                expires_at=expiry,
                diagnostics=[
                    ExecutorDiagnostic(
                        executor=executor,
                        enabled=True,
                        auth_mode=ExecutorAuthMode.HOST_SESSION,
                        availability=ExecutorAvailability.AVAILABLE,
                        observed_at=now,
                        expires_at=expiry,
                        active_lease_count=0,
                        reason_code="ready",
                        reason="Ready.",
                    )
                    for executor in (AgentType.CLAUDE, AgentType.CODEX)
                ],
            ).model_dump_json(),
            ex=3600,
        )
    finally:
        await redis.aclose()

    # Reset internal client to avoid Event Loop Closed errors across tests
    client._client = None
    yield client
    await client.close()
