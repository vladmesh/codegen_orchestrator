"""Service coverage for an engineering result that carried no new commit.

The stand case this reproduces: a taskless deploy-fix worker finished reporting
the SHA already deployed for the story. The consumer must end that attempt as a
failed Run with its own reason, publish no deploy, and take the story out of
`in_progress` — where `complete_stories` would otherwise retry a pull request
GitHub refuses with 422 "No commits between" for ever.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import EngineeringFailureReason
from shared.contracts.vocab import AgentType
from shared.contracts.worker_turn import AttemptTurnMetadata
from shared.queues import DEPLOY_QUEUE
from shared.redis import RedisStreamClient
from tests.unit.factories import make_project, make_repository

_DEPLOYED_HEAD = "d159f7d"
_STORY_ID = "story-1"
_ATTEMPT_ID = "eng-deploy-fix-deploy-poll-0a09250a-1"


def _engineering_message() -> dict:
    """A taskless deploy-fix job: story-owned, no planning task, deploy expected."""
    return {
        "task_id": _ATTEMPT_ID,
        "project_id": str(make_project().id),
        "initiating_run_id": "live-run-1",
        "telegram_chat_id": "",
        "action": "fix",
        "description": "Repair the failing deploy",
        "skip_deploy": False,
        "planning_task_id": None,
        "story_id": _STORY_ID,
        "deploy_fix_attempt": 1,
    }


async def _github_reporting_a_deployed_head():
    """A repository whose default branch already carries the reported commit."""
    client = AsyncMock()
    client.get_repo_scoped_token = AsyncMock(return_value="ghs_fake")
    client.get_repo = AsyncMock(return_value=SimpleNamespace(default_branch="main"))
    client.branch_contains_commit = AsyncMock(return_value=True)
    return client


@pytest.mark.asyncio
async def test_reported_deployed_head_fails_the_run_and_parks_the_story(real_redis):
    """The whole consumer path, from worker result to Run, story and deploy queue."""
    from src.clients.worker_spawner import SpawnResult
    from src.consumers.engineering import process_engineering_job

    await real_redis.delete(DEPLOY_QUEUE)

    api = AsyncMock()
    api.get_project = AsyncMock(return_value=make_project())
    api.get_primary_repository = AsyncMock(return_value=make_repository())
    api.get_run = AsyncMock(return_value=SimpleNamespace(run_metadata={}))
    api.transition_story = AsyncMock()

    github = await _github_reporting_a_deployed_head()
    redis_client = RedisStreamClient()
    await redis_client.connect()

    try:
        with (
            patch("src.consumers.engineering.api_client", api),
            patch("src.consumers.engineering_result_handler.api_client", api),
            patch("src.nodes.developer.api_client", api),
            patch("src.nodes.developer.GitHubAppClient", return_value=github),
            patch(
                "src.consumers.engineering._load_engineering_executor_decision",
                new=AsyncMock(
                    return_value=ExecutorDecision(
                        attempt_kind=RunType.ENGINEERING,
                        agent_type=AgentType.CLAUDE,
                        source=ExecutorDecisionSource.API_DEFAULT,
                        policy_version="v1",
                        reason="Engineering executor selected by API DEFAULT_AGENT_TYPE.",
                    )
                ),
            ),
            patch(
                "src.consumers.engineering._resolve_allocations",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "src.consumers.engineering._recorded_attempt_turn",
                new=AsyncMock(return_value=AttemptTurnMetadata()),
            ),
            patch(
                "src.consumers.engineering._build_story_context", new=AsyncMock(return_value=None)
            ),
            patch("src.consumers.engineering._build_story_md", new=AsyncMock(return_value=None)),
            patch(
                "src.nodes.developer.request_spawn",
                new=AsyncMock(
                    return_value=SpawnResult(
                        request_id="req-1",
                        success=True,
                        exit_code=0,
                        output="The deployment already looks correct",
                        commit_sha=_DEPLOYED_HEAD,
                    )
                ),
            ),
        ):
            outcome = await process_engineering_job(_engineering_message(), redis_client)

        assert outcome["status"] == "failed"

        run_patches = [
            call for call in api.patch.await_args_list if call.args[0] == f"runs/{_ATTEMPT_ID}"
        ]
        terminal = run_patches[-1].kwargs["json"]
        assert terminal["status"] == "failed"
        assert terminal["result"]["failure_reason"] == EngineeringFailureReason.NO_NEW_COMMIT.value
        assert "no new commit" in terminal["error_message"]

        story_patches = [
            call for call in api.patch.await_args_list if call.args[0] == f"stories/{_STORY_ID}"
        ]
        assert len(story_patches) == 1
        assert story_patches[0].kwargs["json"]["quarantine_reason"]["reason"] == "no_new_commit"
        api.transition_story.assert_awaited_once_with(_STORY_ID, "human-review")

        # Nothing was handed to deploy: the SHA is already deployed.
        assert await real_redis.xlen(DEPLOY_QUEUE) == 0
    finally:
        await redis_client.close()
        await real_redis.delete(DEPLOY_QUEUE)
