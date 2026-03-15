"""Tests for engineering worker reuse."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.project import ProjectStatus
from src.clients.worker_spawner import SpawnResult


def _project(**overrides):
    """Minimal project dict for tests."""
    base = {
        "id": "proj-1",
        "name": "test-project",
        "config": {"modules": ["backend"]},
    }
    base.update(overrides)
    return base


class TestDeveloperNodeWorkerId:
    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_success_includes_worker_id(self, mock_github_cls, mock_api, mock_spawn):
        """DeveloperNode should include worker_id from SpawnResult on success."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="All done!",
            commit_sha="abc123",
            worker_id="dev-test-abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(
            {
                "project_spec": _project(status=ProjectStatus.ACTIVE.value),
                "action": "create",
                "errors": [],
            }
        )

        assert result["engineering_status"] == "done"
        assert result["worker_id"] == "dev-test-abc123"
