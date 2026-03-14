"""Tests for deploy → engineering feedback loop.

When deploy or smoke fails due to a code bug, the deploy worker should
re-dispatch a fix task to engineering:queue so the developer can fix the issue.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger
from shared.queues import ENGINEERING_QUEUE


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
    r.redis.delete = AsyncMock()
    r.redis.incr = AsyncMock(return_value=1)
    r.redis.expire = AsyncMock()
    r.publish_flat = AsyncMock()
    r.publish_message = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    with patch("src.consumers.deploy.api_client") as api:
        api.patch = AsyncMock()
        api.get = AsyncMock(return_value=[])
        api.post = AsyncMock()
        api.transition_story = AsyncMock()
        api.get_project = AsyncMock(
            return_value={
                "id": "proj-1",
                "name": "my-project",
                "config": {"modules": ["backend", "tg_bot"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/my-project"}
        )
        yield api


@pytest.fixture
def mock_allocations():
    mock_fn = AsyncMock(return_value={"server_ip": "1.2.3.4", "port": 8080})
    with (
        patch("src.tools.allocator.ensure_project_allocations", mock_fn),
        patch("src.tools.allocator.AllocationError", Exception),
    ):
        yield mock_fn


@pytest.fixture
def mock_devops_subgraph():
    with patch("src.consumers.deploy.create_devops_subgraph") as factory:
        graph = AsyncMock()
        factory.return_value = graph
        yield graph


def _job(*, story_id="story-1", user_id="12345", deploy_fix_attempt=0):
    return {
        "task_id": "deploy-1",
        "project_id": "proj-1",
        "user_id": user_id,
        "callback_stream": "",
        "story_id": story_id,
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "deploy_fix_attempt": deploy_fix_attempt,
    }


class TestSmokeFailureRedispatch:
    """When smoke test fails, deploy worker should re-dispatch to engineering."""

    @pytest.mark.asyncio
    async def test_smoke_failure_publishes_engineering_fix(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Smoke failure should publish a fix task to engineering:queue."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": "http://1.2.3.4:8080",
                "deployment_result": {},
                "smoke_result": {
                    "status": "fail",
                    "checks": [
                        {"module": "tg_bot", "result": "fail", "detail": "No response within 15s"},
                    ],
                },
                "errors": ["Smoke failed: tg_bot check — No response within 15s"],
            }
        )

        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

        assert result["status"] == "failed"

        # Should publish engineering fix task
        eng_calls = [
            c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        assert len(eng_calls) == 1, "Smoke failure must re-dispatch a fix task to engineering:queue"

        eng_msg = eng_calls[0][0][1]
        assert eng_msg.action == "fix"
        assert eng_msg.project_id == "proj-1"
        assert "smoke" in eng_msg.description.lower() or "tg_bot" in eng_msg.description.lower()

    @pytest.mark.asyncio
    async def test_smoke_failure_includes_error_context_in_description(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Engineering fix description should contain the smoke failure details."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": "http://1.2.3.4:8080",
                "deployment_result": {},
                "smoke_result": {
                    "status": "fail",
                    "checks": [
                        {
                            "module": "backend",
                            "result": "fail",
                            "detail": "HTTP 502 Bad Gateway",
                        },
                    ],
                },
                "errors": ["Smoke failed: backend health check — HTTP 502 Bad Gateway"],
            }
        )

        from src.consumers.deploy import process_deploy_job

        await process_deploy_job(_job(), mock_redis)

        eng_calls = [
            c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        eng_msg = eng_calls[0][0][1]
        assert "502" in eng_msg.description or "Bad Gateway" in eng_msg.description


class TestDeployWorkflowFailureRedispatch:
    """When deploy workflow fails (no deployed_url), re-dispatch to engineering."""

    @pytest.mark.asyncio
    async def test_deploy_failure_publishes_engineering_fix(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Deploy workflow failure should publish a fix task to engineering:queue."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": None,
                "deployment_result": {"status": "failed", "error": "Container tg_bot crashed"},
                "smoke_result": None,
                "errors": ["Deploy workflow failed: Container tg_bot exited with code 1"],
            }
        )

        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

        assert result["status"] == "failed"

        eng_calls = [
            c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        assert len(eng_calls) == 1, (
            "Deploy failure must re-dispatch a fix task to engineering:queue"
        )

        eng_msg = eng_calls[0][0][1]
        assert eng_msg.action == "fix"
        assert eng_msg.project_id == "proj-1"

    @pytest.mark.asyncio
    async def test_deploy_failure_does_not_redispatch_for_missing_secrets(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Missing secrets is NOT a code bug — should NOT re-dispatch to engineering."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": None,
                "missing_user_secrets": ["TELEGRAM_BOT_TOKEN"],
                "errors": [],
            }
        )

        from src.consumers.deploy import process_deploy_job

        await process_deploy_job(_job(), mock_redis)

        eng_calls = [
            c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        assert len(eng_calls) == 0, "Missing secrets must NOT trigger engineering re-dispatch"


class TestDeployFixRetryLimit:
    """Deploy→engineering loop must have a retry limit to prevent infinite cycles."""

    @pytest.mark.asyncio
    async def test_max_retries_stops_redispatch(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """After MAX deploy fix attempts, should NOT re-dispatch — just fail."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": "http://1.2.3.4:8080",
                "deployment_result": {},
                "smoke_result": {
                    "status": "fail",
                    "checks": [
                        {"module": "backend", "result": "fail", "detail": "HTTP 500"},
                    ],
                },
                "errors": ["Smoke failed"],
            }
        )

        from src.consumers.deploy import MAX_DEPLOY_FIX_ATTEMPTS, process_deploy_job

        # Set attempt to max — should NOT re-dispatch
        job = _job(deploy_fix_attempt=MAX_DEPLOY_FIX_ATTEMPTS)
        result = await process_deploy_job(job, mock_redis)

        assert result["status"] == "failed"

        eng_calls = [
            c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        assert len(eng_calls) == 0, (
            f"After {MAX_DEPLOY_FIX_ATTEMPTS} attempts, must NOT re-dispatch"
        )

    @pytest.mark.asyncio
    async def test_attempt_counter_increments(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Re-dispatched engineering message should have incremented attempt counter."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": "http://1.2.3.4:8080",
                "deployment_result": {},
                "smoke_result": {
                    "status": "fail",
                    "checks": [
                        {"module": "backend", "result": "fail", "detail": "HTTP 500"},
                    ],
                },
                "errors": ["Smoke failed"],
            }
        )

        from src.consumers.deploy import process_deploy_job

        job = _job(deploy_fix_attempt=1)
        await process_deploy_job(job, mock_redis)

        eng_calls = [
            c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        assert len(eng_calls) == 1
        eng_msg = eng_calls[0][0][1]
        assert eng_msg.deploy_fix_attempt == 2  # noqa: PLR2004


class TestEngineringMessagePassthrough:
    """Engineering worker should pass deploy_fix_attempt through to deploy."""

    @pytest.mark.asyncio
    async def test_engineering_passes_attempt_to_deploy(self):
        """When engineering triggers deploy, deploy_fix_attempt should carry over."""
        with (
            patch("src.consumers.engineering.api_client") as api,
            patch("src.subgraphs.engineering.create_engineering_subgraph") as factory,
            patch("src.consumers.engineering.resource_allocator_node") as mock_alloc,
            patch("src.consumers.engineering.get_story_worker", return_value=None),
            patch("src.consumers.engineering._wait_for_ci_and_fix") as ci_gate,
            patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock),
            patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock),
        ):
            api.patch = AsyncMock()
            api.post = AsyncMock()
            api.get = AsyncMock(return_value=[])
            api.get_project = AsyncMock(
                return_value={
                    "id": "proj-1",
                    "name": "test",
                    "config": {"modules": ["backend"]},
                }
            )
            api.get_primary_repository = AsyncMock(
                return_value={"id": "repo-1", "git_url": "https://github.com/org/test"}
            )
            api.get_tasks_by_story = AsyncMock(return_value=[])
            mock_alloc.run = AsyncMock(return_value={"allocated_resources": {}, "errors": []})

            graph = AsyncMock()
            graph.ainvoke = AsyncMock(
                return_value={
                    "engineering_status": "done",
                    "commit_sha": "abc123",
                    "worker_id": "w-1",
                }
            )
            factory.return_value = graph

            ci_gate.return_value = (True, [], False, None)

            mock_redis = AsyncMock()
            mock_redis.redis = AsyncMock()
            mock_redis.publish_flat = AsyncMock()
            mock_redis.publish_message = AsyncMock()

            from src.consumers.engineering import process_engineering_job

            job = {
                "task_id": "eng-fix-1",
                "project_id": "proj-1",
                "user_id": "12345",
                "action": "fix",
                "deploy_fix_attempt": 2,
                "skip_deploy": False,
            }

            await process_engineering_job(job, mock_redis)

            # Check that deploy message carries the attempt counter
            deploy_calls = [
                c for c in mock_redis.publish_message.call_args_list if c[0][0] == "deploy:queue"
            ]
            assert len(deploy_calls) == 1
            deploy_msg = deploy_calls[0][0][1]
            assert deploy_msg.deploy_fix_attempt == 2  # noqa: PLR2004
