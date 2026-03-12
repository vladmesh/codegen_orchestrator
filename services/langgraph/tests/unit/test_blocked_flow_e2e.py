"""End-to-end unit test: developer blocker → WHR flow.

Traces the full path from worker output containing ## BLOCKED through
the developer node and into the engineering consumer handler.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestBlockedFlowEndToEnd:
    """Full blocked path: worker output → developer node → consumer handler."""

    @pytest.mark.asyncio
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    @patch("src.nodes.developer.request_spawn")
    async def test_developer_node_returns_developer_blocked(
        self, mock_spawn, mock_github_cls, mock_api
    ):
        """Developer node sets engineering_status=developer_blocked when worker reports blocker."""
        from src.clients.worker_spawner import SpawnResult
        from src.nodes.developer import DeveloperNode

        # Simulate worker returning a block_reason
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=False,
            exit_code=0,
            output="## BLOCKED\nMissing API credentials for Stripe",
            commit_sha=None,
            worker_id="w-1",
            error_message=None,
            block_reason="Missing API credentials for Stripe",
        )

        mock_api.get_project = AsyncMock(
            return_value={"id": "proj-1", "status": "active", "config": {}}
        )
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-repo"}
        )
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghp_test")

        node = DeveloperNode()
        result = await node.run(
            {
                "project_spec": {
                    "id": "proj-1",
                    "name": "test-project",
                    "status": "active",
                    "config": {"description": "Test", "modules": ["backend"]},
                },
                "action": "feature",
                "description": "Add payment processing",
            }
        )

        assert result["engineering_status"] == "developer_blocked"
        assert result["block_reason"] == "Missing API credentials for Stripe"
        assert result["worker_id"] == "w-1"

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.notify_admins", new_callable=AsyncMock)
    @patch("src.consumers.engineering.publish_story_event", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    async def test_handle_worker_blocked_full_chain(self, mock_api, mock_po_event, mock_notify):
        """_handle_worker_blocked transitions task+story to WHR, notifies admin and user."""
        from src.consumers.engineering import _handle_worker_blocked

        mock_api.patch = AsyncMock()
        mock_api.post = AsyncMock()
        mock_api.get = AsyncMock(return_value={"created_by": "user"})
        mock_notify.return_value = 1

        redis = AsyncMock()
        redis.redis = AsyncMock()
        redis.redis.xadd = AsyncMock()
        redis.publish_flat = AsyncMock()

        result = await _handle_worker_blocked(
            task_id="eng-1",
            project_id="proj-1",
            planning_task_id="task-1",
            story_id="story-1",
            block_reason="56/78 image URLs return 404",
            user_id="u-1",
            redis=redis,
        )

        # Returns blocked status
        assert result["status"] == "blocked"
        assert "56/78" in result["block_reason"]

        # Task transitioned to WHR
        task_transitions = [c for c in mock_api.post.call_args_list if "transition" in str(c)]
        assert any(
            c[1].get("params", {}).get("to_status") == "waiting_human_review"
            for c in task_transitions
        )

        # Task failure_metadata set
        task_patches = [
            c
            for c in mock_api.patch.call_args_list
            if "tasks" in str(c) and "failure_metadata" in str(c)
        ]
        assert len(task_patches) >= 1
        metadata = task_patches[0][1]["json"]["failure_metadata"]
        assert metadata["failure_reason"] == "developer_blocked"

        # Story transitioned to WHR
        story_patches = [c for c in mock_api.patch.call_args_list if "stories" in str(c)]
        assert len(story_patches) >= 1
        assert story_patches[0][1]["json"]["status"] == "waiting_human_review"

        # Admin notified at warning level
        mock_notify.assert_awaited_once()
        assert mock_notify.call_args[1]["level"] == "warning"

        # User notified via PO
        mock_po_event.assert_awaited_once()
        assert mock_po_event.call_args[1]["event"] == "story_blocked"
