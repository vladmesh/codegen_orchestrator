"""Tests for deploy action auto-fallback.

When action=create but service dir already exists on the server,
the deploy worker should auto-fallback to action=feature instead of failing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger


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
                "config": {"modules": ["backend"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/my-project"}
        )
        api.get_server_ssh_key = AsyncMock(return_value="fake-ssh-key")
        yield api


@pytest.fixture
def mock_allocations():
    mock_fn = AsyncMock(
        return_value={
            "backend": {
                "server_ip": "1.2.3.4",
                "server_handle": "srv-1",
                "port": 8080,
            }
        }
    )
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


def _job(*, action="create", story_id="story-1", user_id="12345"):
    return {
        "task_id": "deploy-1",
        "project_id": "proj-1",
        "user_id": user_id,
        "callback_stream": "",
        "story_id": story_id,
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "action": action,
    }


class TestDeployActionFallback:
    """When action=create but dir exists, auto-fallback to feature."""

    @pytest.mark.asyncio
    async def test_create_with_existing_dir_falls_back_to_feature(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """action=create + dir exists should auto-switch to feature, not fail."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": "http://1.2.3.4:8080",
                "deployment_result": {},
                "smoke_result": {"status": "pass", "checks": []},
                "errors": [],
            }
        )

        with patch("src.consumers.deploy._pre_check_server") as mock_precheck:
            # Simulate: first call with action=create returns "dir exists" error,
            # second call with action=feature returns None (OK)
            mock_precheck.side_effect = [
                (
                    "Service dir /opt/services/my-project/ already exists on 1.2.3.4. "
                    "Clean up the previous deployment or use action='feature'."
                ),
                None,
            ]

            from src.consumers.deploy import process_deploy_job

            result = await process_deploy_job(_job(action="create"), mock_redis)

        # Should succeed — fallback worked
        assert result["status"] == "success"
        assert result["deployed_url"] == "http://1.2.3.4:8080"

    @pytest.mark.asyncio
    async def test_create_with_existing_dir_does_not_fail(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Precheck failure on create+dir_exists must NOT propagate as deploy failure."""
        with patch("src.consumers.deploy._pre_check_server") as mock_precheck:
            # First call (create) returns "already exists", second call (feature) passes
            mock_precheck.side_effect = [
                (
                    "Service dir /opt/services/my-project/ already exists on 1.2.3.4. "
                    "Clean up the previous deployment or use action='feature'."
                ),
                None,
            ]

            mock_devops_subgraph.ainvoke = AsyncMock(
                return_value={
                    "deployed_url": "http://1.2.3.4:8080",
                    "deployment_result": {},
                    "smoke_result": None,
                    "errors": [],
                }
            )

            from src.consumers.deploy import process_deploy_job

            result = await process_deploy_job(_job(action="create"), mock_redis)

        assert result["status"] != "failed", "Should auto-fallback, not fail"

    @pytest.mark.asyncio
    async def test_feature_with_missing_dir_still_fails(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """action=feature + dir missing should still fail (no fallback for this case)."""
        with patch("src.consumers.deploy._pre_check_server") as mock_precheck:
            mock_precheck.return_value = (
                "Service dir /opt/services/my-project/ not found on 1.2.3.4. "
                "Project was never deployed. Use action='create' for first deploy."
            )

            from src.consumers.deploy import process_deploy_job

            result = await process_deploy_job(_job(action="feature"), mock_redis)

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_fallback_logs_warning(
        self, mock_redis, mock_api, mock_allocations, mock_devops_subgraph
    ):
        """Auto-fallback should log a warning about the action change."""
        mock_devops_subgraph.ainvoke = AsyncMock(
            return_value={
                "deployed_url": "http://1.2.3.4:8080",
                "deployment_result": {},
                "smoke_result": None,
                "errors": [],
            }
        )

        with (
            patch("src.consumers.deploy._pre_check_server") as mock_precheck,
            patch("src.consumers.deploy.logger") as mock_logger,
        ):
            mock_precheck.side_effect = [
                (
                    "Service dir /opt/services/my-project/ already exists on 1.2.3.4. "
                    "Clean up the previous deployment or use action='feature'."
                ),
                None,
            ]

            from src.consumers.deploy import process_deploy_job

            await process_deploy_job(_job(action="create"), mock_redis)

        # Should have logged a warning about the fallback
        warning_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if "fallback" in str(c).lower() or "auto" in str(c).lower()
        ]
        assert len(warning_calls) >= 1, "Should log warning about action auto-fallback"
