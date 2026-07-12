"""Tests for deploy failure outcome storage for dispatcher routing.

Deploy worker stores deploy_outcome and deploy_fix_attempt in run.result.
The dispatcher reads these to decide whether to redispatch to engineering.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.factories import make_project, make_repository

from shared.contracts.queues.deploy import DeployOutcome, DeployTrigger


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
    r.redis.delete = AsyncMock()
    r.publish_flat = AsyncMock()
    r.publish_message = AsyncMock()
    return r


def _configure_api_mock(api):
    """Configure common API mock methods."""
    api.patch = AsyncMock()
    api.get = AsyncMock(return_value=[])
    api.post = AsyncMock()
    api.get_project = AsyncMock(
        return_value=make_project(
            name="my-project",
            config={"modules": ["backend", "tg_bot"]},
        )
    )
    api.get_primary_repository = AsyncMock(
        return_value=make_repository(git_url="https://github.com/org/my-project")
    )


@pytest.fixture
def mock_api():
    api = AsyncMock()
    _configure_api_mock(api)
    with (
        patch("src.consumers.deploy.api_client", api),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        patch("src.consumers.deploy_result_handler.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
    ):
        yield api


@pytest.fixture
def mock_allocations():
    mock_fn = AsyncMock(return_value={"server_ip": "1.2.3.4", "port": 8080})
    with (
        patch("src.allocations.ensure_project_allocations", mock_fn),
        patch("src.allocations.AllocationError", Exception),
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


def _find_failed_patches(mock_api):
    """Find PATCH calls that set status=failed and return their result dicts."""
    return [
        c[1]["json"]["result"]
        for c in mock_api.patch.call_args_list
        if c[1].get("json", {}).get("status") == "failed" and "result" in c[1].get("json", {})
    ]


class TestSmokeFailureOutcome:
    """Smoke failure stores classified deploy_outcome for dispatcher."""

    @pytest.mark.asyncio
    async def test_smoke_failure_stores_code_fix_outcome(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Smoke failure classified as CODE_FIX stores outcome in run.result."""
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
                "errors": ["Smoke failed"],
            }
        )

        with patch(
            "src.consumers.deploy_result_handler._classify_deploy_failure",
            AsyncMock(return_value="CODE_FIX"),
        ):
            from src.consumers.deploy import process_deploy_job

            result = await process_deploy_job(_job(), mock_redis)

        assert result["status"] == "failed"
        results = _find_failed_patches(mock_api)
        assert any(r["deploy_outcome"] == DeployOutcome.CODE_FIX.value for r in results)

    @pytest.mark.asyncio
    async def test_smoke_failure_stores_error_details(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Smoke failure stores error details for dispatcher to use."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": "http://1.2.3.4:8080",
                "deployment_result": {},
                "smoke_result": {
                    "status": "fail",
                    "checks": [
                        {"module": "backend", "result": "fail", "detail": "HTTP 502 Bad Gateway"},
                    ],
                },
            }
        )

        with patch(
            "src.consumers.deploy_result_handler._classify_deploy_failure",
            AsyncMock(return_value="CODE_FIX"),
        ):
            from src.consumers.deploy import process_deploy_job

            await process_deploy_job(_job(), mock_redis)

        results = _find_failed_patches(mock_api)
        assert any("502" in r.get("error_details", "") for r in results)


class TestDeployWorkflowFailureOutcome:
    """Deploy workflow failure stores classified outcome for dispatcher."""

    @pytest.mark.asyncio
    async def test_deploy_failure_stores_code_fix_outcome(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Deploy workflow failure classified as CODE_FIX stores outcome."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": None,
                "deployment_result": {},
                "smoke_result": None,
                "errors": ["Container tg_bot exited with code 1"],
            }
        )

        with patch(
            "src.consumers.deploy._classify_deploy_failure",
            AsyncMock(return_value="CODE_FIX"),
        ):
            from src.consumers.deploy import process_deploy_job

            result = await process_deploy_job(_job(), mock_redis)

        assert result["status"] == "failed"
        results = _find_failed_patches(mock_api)
        assert any(r["deploy_outcome"] == DeployOutcome.CODE_FIX.value for r in results)

    @pytest.mark.asyncio
    async def test_missing_secrets_stores_give_up_outcome(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Missing secrets should store GIVE_UP outcome (not a code bug)."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": None,
                "missing_user_secrets": ["TELEGRAM_BOT_TOKEN"],
                "errors": [],
            }
        )

        from src.consumers.deploy import process_deploy_job

        await process_deploy_job(_job(), mock_redis)

        results = _find_failed_patches(mock_api)
        assert any(r["deploy_outcome"] == DeployOutcome.GIVE_UP.value for r in results)


class TestDeployFixAttempt:
    """deploy_fix_attempt is stored in run.result for dispatcher routing."""

    @pytest.mark.asyncio
    async def test_attempt_counter_stored_in_result(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """deploy_fix_attempt from message is stored in run.result."""
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
            }
        )

        with patch(
            "src.consumers.deploy_result_handler._classify_deploy_failure",
            AsyncMock(return_value="CODE_FIX"),
        ):
            from src.consumers.deploy import process_deploy_job

            job = _job(deploy_fix_attempt=1)
            await process_deploy_job(job, mock_redis)

        results = _find_failed_patches(mock_api)
        assert any(r.get("deploy_fix_attempt") == 1 for r in results)


class TestEngineringMessagePassthrough:
    """Engineering worker should pass deploy_fix_attempt through to deploy."""

    @pytest.mark.asyncio
    async def test_engineering_passes_attempt_to_deploy(self):
        """When engineering triggers deploy, deploy_fix_attempt should carry over."""
        with (
            patch("src.consumers.engineering.api_client") as api,
            patch("src.consumers.engineering_result_handler.api_client") as rh_api,
            patch("src.subgraphs.engineering.create_engineering_subgraph") as factory,
            patch("src.consumers.engineering.resource_allocator_node") as mock_alloc,
            patch("src.consumers.engineering.get_story_worker", return_value=None),
            patch(
                "src.consumers.engineering_result_handler.set_story_worker", new_callable=AsyncMock
            ),
            patch("src.consumers.engineering_result_handler.delete_worker", new_callable=AsyncMock),
            patch(
                "src.consumers.engineering_result_handler.publish_callback_event",
                new_callable=AsyncMock,
            ),
        ):
            api.patch = AsyncMock()
            api.post = AsyncMock()
            api.get = AsyncMock(return_value=[])
            api.get_project = AsyncMock(
                return_value=make_project(
                    name="test",
                    config={"modules": ["backend"]},
                )
            )
            api.get_primary_repository = AsyncMock(
                return_value=make_repository(git_url="https://github.com/org/test")
            )
            api.get_tasks_by_story = AsyncMock(return_value=[])
            rh_api.patch = AsyncMock()
            rh_api.post = AsyncMock()
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
