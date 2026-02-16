"""Unit tests for DeveloperNode commit_sha validation.

Verifies that the developer node rejects success results without a commit_sha,
preventing the cascade failure where commit_sha=None propagates unchecked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.clients.worker_spawner import SpawnResult


def _make_state(*, action="create", status="scaffolded"):
    return {
        "project_spec": {
            "id": "proj-1",
            "name": "test-project",
            "status": status,
            "config": {"modules": ["backend"]},
            "repository_url": "https://github.com/org/test-project",
        },
        "action": action,
        "errors": [],
    }


class TestDeveloperNodeCommitValidation:
    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_success_without_commit_sha_returns_blocked(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """Worker success=True but commit_sha=None must return blocked, not done."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="All done!",
            commit_sha=None,
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(_make_state())

        assert result["engineering_status"] == "blocked"
        assert any("no commit" in e.lower() for e in result["errors"])

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_success_with_commit_sha_returns_done(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """Worker success=True with commit_sha must return done."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="All done!",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(_make_state())

        assert result["engineering_status"] == "done"
        assert result["commit_sha"] == "abc123"

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_failure_still_returns_blocked(self, mock_github_cls, mock_api, mock_spawn):
        """Worker success=False must return blocked (existing behavior, sanity check)."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=False,
            exit_code=1,
            output="",
            error_message="timeout",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(_make_state())

        assert result["engineering_status"] == "blocked"
