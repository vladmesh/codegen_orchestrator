"""Stopping earlier deploy workflows checks the deploy lock before every cancellation.

`fence_active_deploys` runs the real `GitHubAppClient.fence_workflow` against a
GitHub Actions fake served through `respx`; the deploy lock is an in-memory fake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from shared.clients.github import GitHubAppClient
from src.deploy_fence import DeployFence, DeployFenceLost
from src.subgraphs.devops.deployer import fence_active_deploys

HOLDER = "deploy-a:1"


class Lock:
    def __init__(self) -> None:
        self.value: str | None = HOLDER

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        return int(self.value == token)


class Actions:
    """Two unfinished runs; a cancelled run reads completed afterwards."""

    def __init__(self, lock: Lock) -> None:
        self.runs = {41: "in_progress", 42: "queued"}
        self.cancelled: list[int] = []
        self.lose_lock_on_cancel = False
        self.lock = lock

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/runs"):
            status = request.url.params["status"]
            listed = [{"id": i, "status": s} for i, s in self.runs.items() if s == status]
            return httpx.Response(200, json={"workflow_runs": listed})
        if path.endswith("/cancel"):
            run_id = int(path.split("/")[-2])
            self.cancelled.append(run_id)
            self.runs[run_id] = "completed"
            if self.lose_lock_on_cancel:
                self.lock.value = "deploy-b:1"
            return httpx.Response(202)
        run_id = int(path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"id": run_id, "status": self.runs[run_id]})


async def _fence(actions: Actions, lock: Lock) -> list[int]:
    github = GitHubAppClient()
    github.get_token = AsyncMock(return_value="github-token")
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="api.github.com").mock(side_effect=actions.handle)
        return await fence_active_deploys(
            github, "org", "product", "proj-1", DeployFence(lock, "proj-1", HOLDER)
        )


@pytest.mark.asyncio
async def test_a_held_lock_stops_every_unfinished_run():
    lock = Lock()
    actions = Actions(lock)

    assert sorted(await _fence(actions, lock)) == [41, 42]
    assert sorted(actions.cancelled) == [41, 42]


@pytest.mark.asyncio
async def test_a_lock_lost_between_cancellations_refuses_the_next_one_as_lock_lost():
    lock = Lock()
    actions = Actions(lock)
    actions.lose_lock_on_cancel = True

    with pytest.raises(DeployFenceLost):
        await _fence(actions, lock)

    assert len(actions.cancelled) == 1
