"""Tests for story-level worker reuse — spawn once per story, reuse for subsequent tasks."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.worker_spawner import SpawnResult


def _project(**overrides):
    """Minimal project dict for tests."""
    base = {
        "id": "proj-1",
        "name": "test-project",
        "config": {"modules": ["backend"]},
        "status": "developing",
    }
    base.update(overrides)
    return base


class TestDeveloperNodeWorkerReuse:
    """DeveloperNode uses send_task_to_worker when worker_id in state."""

    @pytest.mark.asyncio
    @patch("src.nodes.developer.send_task_to_worker", new_callable=AsyncMock)
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_reuses_worker_when_id_in_state(
        self, mock_github_cls, mock_api, mock_spawn, mock_send_task
    ):
        """When worker_id is in state, should use send_task_to_worker."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_send_task.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done!",
            commit_sha="def456",
            worker_id="dev-existing-abc",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(
            {
                "project_spec": _project(),
                "action": "feature",
                "description": "Add login page",
                "worker_id": "dev-existing-abc",
                "errors": [],
            }
        )

        assert result["engineering_status"] == "done"
        assert result["worker_id"] == "dev-existing-abc"
        mock_send_task.assert_called_once()
        mock_spawn.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.nodes.developer.send_task_to_worker", new_callable=AsyncMock)
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_falls_back_to_spawn_when_worker_dead(
        self, mock_github_cls, mock_api, mock_spawn, mock_send_task
    ):
        """When send_task_to_worker fails with timeout, fall back to request_spawn."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        # send_task_to_worker times out
        mock_send_task.return_value = SpawnResult(
            request_id="req-1",
            success=False,
            exit_code=-1,
            output="Timeout",
            error_message="execution_timeout",
            worker_id="dev-existing-abc",
        )
        # Fallback to spawn succeeds
        mock_spawn.return_value = SpawnResult(
            request_id="req-2",
            success=True,
            exit_code=0,
            output="Done!",
            commit_sha="ghi789",
            worker_id="dev-new-xyz",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(
            {
                "project_spec": _project(),
                "action": "feature",
                "description": "Add login page",
                "worker_id": "dev-existing-abc",
                "errors": [],
            }
        )

        assert result["engineering_status"] == "done"
        assert result["worker_id"] == "dev-new-xyz"
        mock_send_task.assert_called_once()
        mock_spawn.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_spawns_when_no_worker_id(self, mock_github_cls, mock_api, mock_spawn):
        """When no worker_id in state, should use request_spawn as before."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done!",
            commit_sha="abc123",
            worker_id="dev-fresh-abc",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(
            {
                "project_spec": _project(),
                "action": "feature",
                "description": "Add login page",
                "errors": [],
            }
        )

        assert result["engineering_status"] == "done"
        assert result["worker_id"] == "dev-fresh-abc"
        mock_spawn.assert_called_once()


class TestEngineeringConsumerStoryWorker:
    """process_engineering_job stores/reuses story worker."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.get_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._handle_engineering_success", new_callable=AsyncMock)
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_stores_worker_for_story_after_success(
        self,
        mock_publish,
        mock_api,
        mock_resource,
        mock_create_graph,
        mock_handle_success,
        mock_delete,
        mock_get_worker,
        mock_set_worker,
    ):
        """First task in story: spawn → lookup worker, pass to subgraph."""
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_project_allocations = AsyncMock(return_value=[])
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()
        mock_resource.run = AsyncMock(return_value={"allocated_resources": {}, "errors": []})

        # Subgraph returns done with worker_id
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "abc123",
                "worker_id": "dev-new-abc",
            }
        )
        mock_create_graph.return_value = mock_graph
        mock_handle_success.return_value = {"status": "success"}
        mock_get_worker.return_value = None  # No existing worker

        from src.consumers.engineering import process_engineering_job

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        result = await process_engineering_job(
            {
                "task_id": "eng-123",
                "project_id": "proj-1",
                "user_id": "u-1",
                "action": "feature",
                "description": "Add login",
                "story_id": "story-1",
                "planning_task_id": "task-1",
                "skip_deploy": True,
            },
            redis_mock,
        )

        assert result["status"] == "success"
        # Should look up worker for story
        mock_get_worker.assert_called_once_with(redis_mock.redis, "story-1")
        # story_id should be passed to _handle_engineering_success
        call_kwargs = mock_handle_success.call_args.kwargs
        assert call_kwargs["story_id"] == "story-1"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.get_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._handle_engineering_success", new_callable=AsyncMock)
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_passes_existing_worker_to_subgraph(
        self,
        mock_publish,
        mock_api,
        mock_resource,
        mock_create_graph,
        mock_handle_success,
        mock_delete,
        mock_get_worker,
        mock_set_worker,
    ):
        """Second task in story: lookup existing worker_id, pass to subgraph."""
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_project_allocations = AsyncMock(return_value=[])
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()
        mock_resource.run = AsyncMock(return_value={"allocated_resources": {}, "errors": []})

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "def456",
                "worker_id": "dev-existing-abc",
            }
        )
        mock_create_graph.return_value = mock_graph
        mock_handle_success.return_value = {"status": "success"}
        mock_get_worker.return_value = "dev-existing-abc"  # Existing worker

        from src.consumers.engineering import process_engineering_job

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        await process_engineering_job(
            {
                "task_id": "eng-456",
                "project_id": "proj-1",
                "user_id": "u-1",
                "action": "feature",
                "description": "Add profile page",
                "story_id": "story-1",
                "planning_task_id": "task-2",
                "skip_deploy": True,
            },
            redis_mock,
        )

        # Should pass worker_id to subgraph
        invoke_args = mock_graph.ainvoke.call_args[0][0]
        assert invoke_args["worker_id"] == "dev-existing-abc"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.get_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._handle_engineering_success", new_callable=AsyncMock)
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_no_worker_lookup_for_standalone_task(
        self,
        mock_publish,
        mock_api,
        mock_resource,
        mock_create_graph,
        mock_handle_success,
        mock_delete,
        mock_get_worker,
        mock_set_worker,
    ):
        """Task without story_id: no worker lookup."""
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_project_allocations = AsyncMock(return_value=[])
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()
        mock_resource.run = AsyncMock(return_value={"allocated_resources": {}, "errors": []})

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "abc123",
                "worker_id": "dev-standalone-abc",
            }
        )
        mock_create_graph.return_value = mock_graph
        mock_handle_success.return_value = {"status": "success"}

        from src.consumers.engineering import process_engineering_job

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        await process_engineering_job(
            {
                "task_id": "eng-789",
                "project_id": "proj-1",
                "user_id": "u-1",
                "action": "feature",
                "description": "Standalone fix",
                "skip_deploy": True,
            },
            redis_mock,
        )

        # No story → no worker lookup
        mock_get_worker.assert_not_called()
        # story_id should be None in handle_success
        call_kwargs = mock_handle_success.call_args.kwargs
        assert call_kwargs["story_id"] is None


class TestHandleSuccessWorkerLifecycle:
    """_handle_engineering_success stores or deletes worker based on story_id."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_stores_worker_when_story_id(
        self, mock_publish, mock_api, mock_ci_fix, mock_delete, mock_set_worker
    ):
        """With story_id: store worker in registry, don't delete."""
        mock_ci_fix.return_value = (True, [{"attempt": 0, "status": "passed"}])
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()

        from src.consumers.engineering import _handle_engineering_success

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        await _handle_engineering_success(
            result={"engineering_status": "done", "commit_sha": "abc", "worker_id": "dev-abc"},
            task_id="eng-1",
            project=_project(),
            callback_stream="cb:1",
            redis=redis_mock,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="u-1",
            planning_task_id="task-1",
            story_id="story-1",
        )

        mock_set_worker.assert_called_once_with(redis_mock.redis, "story-1", "dev-abc")
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.set_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_deletes_worker_when_no_story_id(
        self, mock_publish, mock_api, mock_ci_fix, mock_delete, mock_set_worker
    ):
        """Without story_id: delete worker after CI."""
        mock_ci_fix.return_value = (True, [{"attempt": 0, "status": "passed"}])
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()

        from src.consumers.engineering import _handle_engineering_success

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        await _handle_engineering_success(
            result={"engineering_status": "done", "commit_sha": "abc", "worker_id": "dev-abc"},
            task_id="eng-1",
            project=_project(),
            callback_stream="cb:1",
            redis=redis_mock,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="u-1",
        )

        mock_delete.assert_called_once_with("dev-abc", reason="completed")
        mock_set_worker.assert_not_called()
