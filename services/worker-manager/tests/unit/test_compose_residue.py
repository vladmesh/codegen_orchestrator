"""The worker's bounded compose plan leaves no container behind.

`issue:868e40fc0377b0dabb77`: `docker compose down -v` removes the plan's
services and not the one-shot containers `docker compose run` creates beside
them — the generated product's `make test-integration` — so exited
`*-integration-tests-run-*` containers survived a completed story for 7+ hours
and then survived a whole project teardown. Both halves of the fix are here: the
worker's own teardown sweeps by the Compose project label, and the orphan
collector sweeps plans whose worker Redis no longer knows.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.worker_compose import COMPOSE_PROJECT_LABEL, worker_compose_project
from src.garbage_collector import (
    _collect_orphaned_compose_containers,
    remove_worker_compose_residue,
)

WORKER = "dev-p-163f3678dc694b9ea2-f0523fb9"
ONE_OFF = f"worker_{WORKER}-integration-tests-run-67ea0c169cf0"


def container(name: str, *, project: str | None = None, status: str = "exited") -> MagicMock:
    made = MagicMock()
    made.name = name
    made.status = status
    made.labels = {} if project is None else {COMPOSE_PROJECT_LABEL: project}
    return made


def docker(containers: list[MagicMock]) -> MagicMock:
    client = MagicMock()
    client.list_containers = AsyncMock(return_value=containers)
    client.remove_container = AsyncMock()
    return client


@pytest.mark.asyncio
class TestTheWorkersOwnTeardown:
    async def test_it_removes_the_exited_one_shot_container_down_left_behind(self):
        client = docker([container(ONE_OFF, project=worker_compose_project(WORKER))])

        removed = await remove_worker_compose_residue(client, WORKER)

        assert removed == [ONE_OFF]
        client.list_containers.assert_awaited_once_with(
            filters={"label": f"{COMPOSE_PROJECT_LABEL}={worker_compose_project(WORKER)}"},
            all=True,
        )
        client.remove_container.assert_awaited_once_with(ONE_OFF, force=True, v=True)

    async def test_an_unavailable_listing_removes_nothing_and_raises_nothing(self):
        """One unreadable inventory must not wedge a worker's teardown."""
        client = docker([])
        client.list_containers = AsyncMock(side_effect=RuntimeError("daemon is gone"))

        assert await remove_worker_compose_residue(client, WORKER) == []
        client.remove_container.assert_not_awaited()

    async def test_one_stuck_container_does_not_stop_the_rest(self):
        client = docker(
            [
                container("stuck", project=worker_compose_project(WORKER)),
                container(ONE_OFF, project=worker_compose_project(WORKER)),
            ]
        )
        client.remove_container = AsyncMock(side_effect=[RuntimeError("in use"), None])

        assert await remove_worker_compose_residue(client, WORKER) == [ONE_OFF]


@pytest.mark.asyncio
class TestTheOrphanCollector:
    async def test_it_removes_a_plan_whose_worker_redis_has_forgotten(self):
        """The case the issue records twice: teardown never ran at all."""
        client = docker([container(ONE_OFF, project=worker_compose_project(WORKER))])

        await _collect_orphaned_compose_containers(client, known_ids=set(), protected_ids=set())

        client.remove_container.assert_awaited_once_with(ONE_OFF, force=True, v=True)

    async def test_it_leaves_a_plan_of_a_worker_redis_still_knows(self):
        client = docker([container(ONE_OFF, project=worker_compose_project(WORKER))])

        await _collect_orphaned_compose_containers(client, known_ids={WORKER}, protected_ids=set())

        client.remove_container.assert_not_awaited()

    async def test_a_live_container_protects_its_worker_rather_than_being_taken(self):
        """Redis having lost a worker is not evidence that its work is garbage."""
        client = docker(
            [container(ONE_OFF, project=worker_compose_project(WORKER), status="running")]
        )
        protected: set[str] = set()

        await _collect_orphaned_compose_containers(client, known_ids=set(), protected_ids=protected)

        client.remove_container.assert_not_awaited()
        assert protected == {WORKER}

    async def test_a_compose_project_that_is_not_a_workers_is_never_touched(self):
        """The orchestrator's own stack and a product deployment carry this label too."""
        client = docker(
            [
                container("codegen-api-1", project="codegen"),
                container("live-test-9-backend-1", project="live-test-9"),
            ]
        )

        await _collect_orphaned_compose_containers(client, known_ids=set(), protected_ids=set())

        client.remove_container.assert_not_awaited()

    async def test_an_unavailable_listing_removes_nothing(self):
        client = docker([])
        client.list_containers = AsyncMock(side_effect=RuntimeError("daemon is gone"))

        await _collect_orphaned_compose_containers(client, known_ids=set(), protected_ids=set())

        client.remove_container.assert_not_awaited()
