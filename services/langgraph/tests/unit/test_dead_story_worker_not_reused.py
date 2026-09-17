"""The production sequence of 2026-09-17: three attempts sent to a deleted worker.

Worker-manager ACKed the spawn of `dev-p-ed689144b9e143b088-2c8b5503`, failed its
checkout, deleted the worker itself — and left the story binding behind, because
`delete_worker` removes exactly the keys (`worker:status`, `worker:meta`) the
registry would have needed to see it was dead. The next two engineering attempts
logged `reusing_story_worker` with that id and sent their task to an input stream
with no consumer; the second one sat there for the whole 4500s turn backstop.

This test drives the real teardown and the real reuse lookup over one fake Redis.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import aioredis
import pytest
from structlog.testing import capture_logs

from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.worker_turn import AttemptTurnMetadata
from src.clients.story_worker_registry import get_story_worker, set_story_worker
from src.consumers import engineering
from tests.unit.factories import make_repository

STORY_ID = "story-ea07a289"
WORKER_ID = "dev-p-ed689144b9e143b088-2c8b5503"
OWNERSHIP = WorkerOwnership(
    story_id=STORY_ID,
    project_id="ed689144-b9e1-43b0-88d5-70187ede4a8c",
    run_id="live-1445",
    attempt_id="eng-50dd6115ecf2",
)


def _load_service_package(name: str, source: Path):
    """Load another service's `src` package without replacing LangGraph's `src`."""
    spec = importlib.util.spec_from_file_location(
        name, source / "__init__.py", submodule_search_locations=[str(source)]
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return package


def _docker_double() -> MagicMock:
    docker = MagicMock()
    docker.inspect_container = AsyncMock(
        return_value={
            "Image": "sha256:" + "1" * 64,
            "Config": {"Image": "worker:latest", "Env": ["WORKER_AGENT_TYPE=codex"]},
            "State": {
                "Status": "running",
                "Running": True,
                "OOMKilled": False,
                "ExitCode": 0,
                "StartedAt": "2026-09-17T18:13:28Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
                "Error": "",
            },
            "Mounts": [],
        }
    )
    docker.read_container_logs = AsyncMock(return_value="checkout_branch_start\n")
    docker.remove_container = AsyncMock()
    docker.remove_network = AsyncMock()
    return docker


@pytest.mark.asyncio
async def test_a_deleted_story_worker_is_never_handed_to_the_next_attempt(monkeypatch):
    root = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("WORKER_BROKER_INTERNAL_TOKEN", "test-broker-token")
    _load_service_package("worker_manager_src", root / "services/worker-manager/src")
    from worker_manager_src.worker_removal import WorkerRemoval  # noqa: PLC0415

    redis = aioredis.FakeRedis(decode_responses=True)
    lock_key = f"workspace:lock:{OWNERSHIP.project_id}"

    # 18:13:28 — the first attempt's worker exists and owns the story.
    await redis.hset(
        f"worker:meta:{WORKER_ID}",
        mapping={"worker_type": "developer", **OWNERSHIP.as_redis_meta()},
    )
    await redis.hset(f"worker:status:{WORKER_ID}", mapping={"status": "RUNNING"})
    await redis.set(lock_key, WORKER_ID)
    await set_story_worker(redis, STORY_ID, WORKER_ID)
    assert await get_story_worker(redis, STORY_ID) == WORKER_ID

    async def release_lock(worker_id: str, project_id: str | None) -> None:
        if project_id and await redis.get(f"workspace:lock:{project_id}") == worker_id:
            await redis.delete(f"workspace:lock:{project_id}")

    # 18:14:00 — the checkout failed after the early ACK and worker-manager
    # deleted the worker it had just created.
    await WorkerRemoval(
        redis,
        _docker_double(),
        unregister_broker_worker=AsyncMock(),
        release_workspace_lock=release_lock,
    ).delete_worker(WORKER_ID, reason="creation_failed")

    assert await redis.exists(f"worker:status:{WORKER_ID}") == 0
    assert await redis.exists(f"worker:meta:{WORKER_ID}") == 0
    assert await redis.get(lock_key) is None

    # 18:14:33 — the next engineering attempt for the same story.
    with capture_logs() as logs:
        resolved = await engineering._existing_attempt_worker(
            SimpleNamespace(redis=redis),
            story_id=STORY_ID,
            task_id="eng-896f6dc42d08",
            attempt_turn=AttemptTurnMetadata(),
        )

    assert resolved is None
    assert [entry for entry in logs if entry["event"] == "reusing_story_worker"] == []


@pytest.mark.asyncio
async def test_the_attempt_that_resolved_no_worker_spawns_a_new_one():
    """The other half of the sequence: no reusable worker means a fresh spawn."""
    from src.clients.worker_spawner import SpawnResult  # noqa: PLC0415
    from src.nodes.developer import DeveloperNode  # noqa: PLC0415

    with (
        patch("src.nodes.developer.GitHubAppClient") as github_cls,
        patch("src.nodes.developer.api_client") as api,
        patch("src.nodes.developer.send_task_to_worker", new_callable=AsyncMock) as send_task,
        patch("src.nodes.developer.request_spawn", new_callable=AsyncMock) as spawn,
    ):
        github_cls.return_value.get_repo_scoped_token = AsyncMock(return_value="ghs_fake")
        api.get_project = AsyncMock(return_value=None)
        api.get_primary_repository = AsyncMock(
            return_value=make_repository(git_url="https://github.com/org/test-project")
        )
        spawn.return_value = SpawnResult(
            request_id="req-2",
            success=True,
            exit_code=0,
            output="Done!",
            commit_sha="abc123",
            worker_id="dev-p-fresh",
        )

        result = await DeveloperNode().run(
            {
                "project_spec": {
                    "id": OWNERSHIP.project_id,
                    "initiating_run_id": OWNERSHIP.run_id,
                    "title": "test-project",
                    "slug": "test-project-0000",
                    "config": {"modules": ["backend"]},
                    "status": "active",
                },
                "action": "feature",
                "run_id": "eng-896f6dc42d08",
                "ownership": OWNERSHIP,
                "description": "Extend the story",
                # What `_existing_attempt_worker` resolved above.
                "worker_id": None,
                "errors": [],
            }
        )

    assert result["worker_id"] == "dev-p-fresh"
    spawn.assert_awaited_once()
    send_task.assert_not_called()
