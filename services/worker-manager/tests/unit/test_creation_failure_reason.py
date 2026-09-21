"""A creation that fails after the early ACK says why it failed.

The create command is ACKed before the heavy work, so nothing downstream can
report the failure: the log and `worker:error:<id>` are the whole account. A
bare timeout — what a checkout that ran out its bound raises — stringifies to
the empty string, and production duly recorded
`worker_creation_failed_after_ack error=''`, which is indistinguishable from no
failure at all. These tests hold the reason non-empty and pinned to a step.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import aioredis
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerCapability,
    WorkerConfig,
    WorkerOwnership,
)
from shared.queues import WORKER_COMMANDS
from src.consumer import WorkerCommandConsumer
from src.creation_failure import mark_worker_creation_step, worker_creation_failure_reason
from src.manager import WorkerManager

pytestmark = pytest.mark.asyncio

_OWNERSHIP = WorkerOwnership(
    story_id="story-ea07a289", project_id="proj-1", run_id="live-1", attempt_id="eng-1"
)


def _docker_mock() -> MagicMock:
    wrapper = MagicMock()
    wrapper.image_exists = AsyncMock(return_value=True)
    wrapper.get_image_label = AsyncMock(return_value="basehash0001")
    wrapper.remove_container = AsyncMock()
    container = MagicMock()
    container.id = "test-id"
    wrapper.run_container = AsyncMock(return_value=container)
    wrapper.exec_in_container = AsyncMock(return_value=(0, b""))
    wrapper.create_network = AsyncMock()
    wrapper.connect_network = AsyncMock()
    wrapper.remove_network = AsyncMock()
    wrapper.get_container_logs = AsyncMock(return_value="")
    return wrapper


async def test_checkout_timeout_after_ack_names_its_step_in_the_worker_error():
    """The 18:13 production failure: an empty-message timeout during checkout."""
    redis = aioredis.FakeRedis(decode_responses=True)
    manager = WorkerManager(redis=redis, docker_client=_docker_mock())

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(
            manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"
        ),
        patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/data/ws/repo-1"), True),
        ),
        patch(
            "src.manager.git_ops.checkout_branch",
            new_callable=AsyncMock,
            # `str()` of this is "" — exactly what production recorded.
            side_effect=TimeoutError(),
        ),
        patch.object(manager, "_register_broker_worker", new_callable=AsyncMock),
        capture_logs() as logs,
    ):
        mock_settings.ENVIRONMENT = "production"
        mock_settings.DOCKER_NETWORK = ""
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_BROKER_URL = "http://worker-broker:8001"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"
        mock_settings.WORKER_TRANSCRIPT_STORAGE_PATH = "/data/worker-transcripts"
        mock_settings.WORKER_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024
        mock_settings.WORKER_TRANSCRIPT_RETENTION_DAYS = 30

        with pytest.raises(TimeoutError):
            await manager.create_worker_with_capabilities(
                worker_id="dev-p-checkout",
                capabilities=["git"],
                base_image="worker-base:latest",
                ownership=_OWNERSHIP,
                agent_type=AgentType.CLAUDE,
                auth_mode="api_key",
                api_key="test-api-key",
                instructions="required instructions",
                repo_id="repo-1",
                branch="story/story-ea07a289",
            )

    recorded = await redis.get("worker:error:dev-p-checkout")
    assert "TimeoutError" in recorded
    assert "checkout_branch" in recorded

    failure = next(entry for entry in logs if entry["event"] == "developer_worker_creation_failed")
    assert failure["step"] == "checkout_branch"
    assert "TimeoutError" in failure["error"]

    # Unchanged: the spawner reads a terminal status and a teardown command.
    assert await redis.hget("worker:status:dev-p-checkout", "status") == WorkerStatus.FAILED
    assert len(await redis.xrange(WORKER_COMMANDS)) == 1


async def test_post_ack_failure_log_is_never_empty():
    """The consumer's own record of a failure it has already ACKed."""
    manager = MagicMock(spec=WorkerManager)
    # What the manager stamps on the way out of the step that failed.
    failure = mark_worker_creation_step(TimeoutError(), "checkout_branch")
    manager.create_worker_with_capabilities = AsyncMock(side_effect=failure)
    client = MagicMock()
    client.publish = AsyncMock()
    consumer = WorkerCommandConsumer(client, manager)

    command = CreateWorkerCommand(
        request_id="eng-50dd6115ecf2",
        config=WorkerConfig(
            name="dev-p-checkout",
            worker_type="developer",
            allowed_commands=[],
            capabilities=[WorkerCapability.GIT],
            agent_type=AgentType.CLAUDE,
            ownership=_OWNERSHIP,
            api_key="test-api-key",
            instructions="required instructions",
            branch="story/story-ea07a289",
        ),
    )

    with capture_logs() as logs:
        assert await consumer._handle_create(command) is None

    # The early ACK is unchanged: the spawner is told the worker id first.
    early = client.publish.await_args_list[0].args[1]
    assert (early["worker_id"], early["success"]) == ("dev-p-checkout", True)

    recorded = next(entry for entry in logs if entry["event"] == "worker_creation_failed_after_ack")
    assert recorded["error"].strip()
    assert "TimeoutError" in recorded["error"]
    assert recorded["step"] == "checkout_branch"


async def test_an_empty_exception_still_has_a_reason():
    assert worker_creation_failure_reason(TimeoutError()) == "TimeoutError"
    assert worker_creation_failure_reason(RuntimeError("no workspace")) == (
        "RuntimeError: no workspace"
    )
