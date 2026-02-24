"""Tests for project_id passthrough from consumer to manager."""

import pytest
from unittest.mock import MagicMock, AsyncMock

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerCapability,
    WorkerConfig,
)

from src.consumer import WorkerCommandConsumer


def _make_create_command(project_id: str | None = None) -> CreateWorkerCommand:
    """Build a CreateWorkerCommand with optional project_id."""
    config = WorkerConfig(
        name="test-worker",
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="test instructions",
        allowed_commands=["*"],
        capabilities=[WorkerCapability.GIT],
        project_id=project_id,
    )
    return CreateWorkerCommand(
        request_id="req-001",
        config=config,
        context={"source": "test"},
    )


@pytest.fixture
def consumer():
    """Consumer with mocked redis and manager."""
    redis = MagicMock()
    redis.xadd = AsyncMock()
    manager = MagicMock()
    manager.create_worker_with_capabilities = AsyncMock(return_value="test-worker")
    return WorkerCommandConsumer(redis=redis, manager=manager)


@pytest.mark.asyncio
async def test_consumer_passes_project_id_to_manager(consumer):
    """project_id from WorkerConfig should be forwarded to manager."""
    cmd = _make_create_command(project_id="proj-123")
    await consumer._handle_create(cmd)

    consumer.manager.create_worker_with_capabilities.assert_awaited_once()
    call_kwargs = consumer.manager.create_worker_with_capabilities.call_args.kwargs
    assert call_kwargs["project_id"] == "proj-123"


@pytest.mark.asyncio
async def test_consumer_passes_none_project_id_when_missing(consumer):
    """When WorkerConfig has no project_id, None should be forwarded."""
    cmd = _make_create_command()  # project_id defaults to None
    await consumer._handle_create(cmd)

    consumer.manager.create_worker_with_capabilities.assert_awaited_once()
    call_kwargs = consumer.manager.create_worker_with_capabilities.call_args.kwargs
    assert call_kwargs["project_id"] is None
