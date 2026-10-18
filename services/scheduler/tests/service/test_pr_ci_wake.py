"""An API PR-review entry wakes the real-Redis PR/CI sweep long before its backstop."""

import asyncio
import os
import uuid

from _scheduler_wake_service import (
    BACKSTOP_SECONDS,
    WAIT_SECONDS,
    call_api,
    story_in_repository,
    wake_channel_subscribed,
)
from _story_scope import OnlyStory
import pytest

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.redis import RedisStreamClient

PR_NUMBER = 42
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


class _PendingGitHub:
    """Reports this repository's PR and CI as still running; every other call is recorded."""

    def __init__(self, repo_name: str) -> None:
        self.repo_name = repo_name
        self.reads: list[tuple] = []
        self.unexpected: list[str] = []

    def __call__(self) -> "_PendingGitHub":
        return self

    async def __aenter__(self) -> "_PendingGitHub":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def _own(self, owner: str, repo_name: str) -> None:
        if (owner, repo_name) != ("test-org", self.repo_name):
            raise RuntimeError("repository outside this test")

    async def get_pull_request(self, owner: str, repo_name: str, pr_number: int) -> dict:
        self._own(owner, repo_name)
        self.reads.append(("pull_request", pr_number))
        return {
            "number": pr_number,
            "node_id": "PR_wake",
            "state": "open",
            "merged_at": None,
            "auto_merge": None,
            "mergeable_state": "blocked",
            "head": {"ref": "story", "sha": HEAD_SHA},
        }

    async def get_latest_workflow_run(
        self, owner: str, repo_name: str, *, workflow_file: str, branch: str
    ) -> dict:
        self._own(owner, repo_name)
        self.reads.append(("workflow_run", workflow_file, branch))
        return {"id": 1, "status": "in_progress", "conclusion": None, "head_sha": HEAD_SHA}

    def __getattr__(self, name: str):
        self.unexpected.append(name)
        raise AttributeError(name)


@pytest.mark.asyncio
async def test_an_api_pr_review_entry_reads_its_pr_and_ci_before_the_backstop(
    api_client, monkeypatch
):
    from src.tasks import pr_ci_loop, pr_poller, scheduler_wake_listener

    repo_name = f"wake-{uuid.uuid4().hex[:10]}"
    github = _PendingGitHub(repo_name)
    monkeypatch.setattr(pr_poller, "GitHubAppClient", github)
    monkeypatch.setattr(pr_ci_loop, "_pr_ci_interval", lambda: BACKSTOP_SECONDS)
    monkeypatch.setattr(scheduler_wake_listener, "_reconnect_interval", lambda: BACKSTOP_SECONDS)
    _project_id, story_id = await story_in_repository(api_client, repo_name, "PR/CI wake")
    await call_api(api_client, "POST", f"stories/{story_id}/start", json={"actor": "test"})
    await call_api(api_client, "PATCH", f"stories/{story_id}", json={"pr_number": PR_NUMBER})
    cycles: asyncio.Queue[dict[str, int]] = asyncio.Queue()
    poll_pr_ci_once = pr_ci_loop.poll_pr_ci_once

    async def observed_cycle(api, redis_client):
        counts = await poll_pr_ci_once(OnlyStory(api, story_id), redis_client)
        cycles.put_nowait(counts)
        return counts

    monkeypatch.setattr(pr_ci_loop, "poll_pr_ci_once", observed_cycle)

    wakes = {target: asyncio.Event() for target in SchedulerWakeTarget}
    observer = RedisStreamClient(os.environ["REDIS_URL"])
    await observer.connect()
    listener = asyncio.create_task(scheduler_wake_listener.scheduler_wake_listener(wakes))
    loop = None
    try:
        await wake_channel_subscribed(observer)
        loop = asyncio.create_task(pr_ci_loop.pr_ci_loop(wake=wakes[SchedulerWakeTarget.PR_CI]))
        assert await asyncio.wait_for(cycles.get(), WAIT_SECONDS) == {
            "prs_merged": 0,
            "ci_failures_routed": 0,
        }
        assert github.reads == []

        await call_api(api_client, "POST", f"stories/{story_id}/pr_review", json={"actor": "test"})

        assert await asyncio.wait_for(cycles.get(), WAIT_SECONDS) == {
            "prs_merged": 0,
            "ci_failures_routed": 0,
        }
        assert github.reads == [
            ("pull_request", PR_NUMBER),
            ("workflow_run", "ci.yml", f"story/{story_id}"),
        ]
        assert github.unexpected == []
        story = await call_api(api_client, "GET", f"stories/{story_id}")
        assert (story["status"], story["pr_number"]) == ("pr_review", PR_NUMBER)
        assert await api_client.get_tasks_by_story(story_id) == []
    finally:
        tasks = [task for task in (loop, listener) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await observer.close()
