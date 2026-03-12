"""Unit tests for DeveloperNode commit_sha validation and repo_id passing.

Verifies that the developer node rejects success results without a commit_sha,
and correctly passes repo_id to request_spawn for workspace mounting.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.project import ProjectStatus
from src.clients.worker_spawner import SpawnResult


def _make_state(*, action="create", status=ProjectStatus.ACTIVE.value, modules=None, repo_id=None):
    return {
        "project_spec": {
            "id": "proj-1",
            "name": "test-project",
            "status": status,
            "config": {
                "modules": modules or ["backend"],
                "description": "A test project",
            },
        },
        "action": action,
        "repo_id": repo_id,
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
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
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
    async def test_success_without_commit_sha_allowed_returns_done(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """Worker success=True, commit_sha=None, allow_no_commit=True → done."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="All tests pass, CI green",
            commit_sha=None,
            worker_id="w-1",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state()
        state["allow_no_commit"] = True
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        assert result["commit_sha"] is None
        assert result["worker_id"] == "w-1"

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_success_with_commit_sha_and_allow_no_commit_returns_done(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """CI-check task that DID make a commit still returns done with commit_sha."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Fixed tests and pushed",
            commit_sha="fix123",
            worker_id="w-1",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state()
        state["allow_no_commit"] = True
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        assert result["commit_sha"] == "fix123"

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_success_with_commit_sha_returns_done(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """Worker success=True with commit_sha must return done."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
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
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
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


class TestRepoIdPassing:
    """Tests that repo_id is correctly passed to request_spawn."""

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_repo_id_from_primary_repo(self, mock_github_cls, mock_api, mock_spawn):
        """repo_id from primary_repo is forwarded to request_spawn."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-abc123", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status=ProjectStatus.ACTIVE.value)
        await node.run(state)

        mock_spawn.assert_awaited_once()
        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["repo_id"] == "repo-abc123"

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_repo_id_fallback_from_state(self, mock_github_cls, mock_api, mock_spawn):
        """repo_id falls back to state when primary_repo has no id."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(
            action="create", status=ProjectStatus.ACTIVE.value, repo_id="repo-from-state"
        )
        await node.run(state)

        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["repo_id"] == "repo-from-state"

    @pytest.mark.asyncio
    async def test_create_with_draft_status_hard_fails(self):
        """action=create + status=draft → hard fail (scaffolder didn't run)."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status="draft")
        result = await node.run(state)

        assert result["engineering_status"] == "blocked"
        assert any("draft" in e for e in result["errors"])

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_create_with_scaffolded_status_proceeds(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """action=create + status=scaffolded → proceeds normally."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status=ProjectStatus.ACTIVE.value)
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        mock_spawn.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_feature_action_passes_repo_id(self, mock_github_cls, mock_api, mock_spawn):
        """action=feature still passes repo_id from primary_repo."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-feat", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="feat123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="feature", status="active")
        state["description"] = "Add feature"
        await node.run(state)

        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["repo_id"] == "repo-feat"


class TestFeatureFlowIntegration:
    """Tests for action=feature/fix through the full DeveloperNode.run() path."""

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_feature_action_skips_scaffold_and_succeeds(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """action=feature on active project → done."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(
            return_value={
                "id": "proj-1",
                "name": "test-project",
                "status": "active",
                "config": {"modules": ["backend"], "description": "A todo API"},
            }
        )
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Feature added",
            commit_sha="feat123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="feature", status="active")
        state["description"] = "Add GET /todos/stats endpoint"
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        assert result["commit_sha"] == "feat123"

        # Verify task_content uses feature template (not create template)
        call_kwargs = mock_spawn.call_args[1]
        assert "existing, working project" in call_kwargs["task_content"]
        assert "Add GET /todos/stats endpoint" in call_kwargs["task_content"]

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_fix_action_uses_fix_template(self, mock_github_cls, mock_api, mock_spawn):
        """action=fix → task title says 'Fix Issue', template says 'existing project'."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Fixed",
            commit_sha="fix456",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="fix", status="active")
        state["description"] = "Fix empty input crash"
        result = await node.run(state)

        assert result["engineering_status"] == "done"
        call_kwargs = mock_spawn.call_args[1]
        assert "Fix issue" in call_kwargs["task_title"]
        assert "Fix empty input crash" in call_kwargs["task_content"]

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_feature_on_scaffolded_project_works(self, mock_github_cls, mock_api, mock_spawn):
        """action=feature on scaffolded (not yet deployed) project works."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc789",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="feature", status=ProjectStatus.ACTIVE.value)
        state["description"] = "Add logging"
        result = await node.run(state)

        assert result["engineering_status"] == "done"

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_feature_refreshes_project_spec(self, mock_github_cls, mock_api, mock_spawn):
        """action=feature refreshes project from API (picks up latest repo URL etc)."""
        fresh_project = {
            "id": "proj-1",
            "name": "test-project",
            "status": "active",
            "config": {"modules": ["backend"], "description": "A test project"},
        }
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=fresh_project)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/updated-repo-name"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="feature", status="active")
        state["description"] = "Some feature"
        await node.run(state)

        # Should have refreshed project spec
        mock_api.get_project.assert_awaited_once_with("proj-1")
        # Repo should use the refreshed URL from primary repository
        call_kwargs = mock_spawn.call_args[1]
        assert "updated-repo-name" in call_kwargs["repo"]


class TestTaskMessageDescription:
    """Tests that _build_create_task reads description from config, not top-level."""

    def test_description_from_config(self):
        """Description in TASK.md must come from config.description."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "name": "test-project",
            "config": {
                "modules": ["backend"],
                "description": "Build a REST API for todos",
            },
        }
        task_md = node._build_create_task(
            project_name="test-project",
            description="Build a REST API for todos",
            modules=["backend"],
            project_spec=project_spec,
        )
        assert "Build a REST API for todos" in task_md

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_config_description_flows_to_task_message(
        self, mock_github_cls, mock_api, mock_spawn
    ):
        """config.description must appear in the task_content passed to request_spawn."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = _make_state(action="create", status=ProjectStatus.ACTIVE.value)
        # Override with specific description
        state["project_spec"]["config"]["description"] = "My specific task with audit"
        await node.run(state)

        mock_spawn.assert_awaited_once()
        task_content = mock_spawn.call_args[1]["task_content"]
        assert "My specific task with audit" in task_content

    def test_create_task_includes_env_hints(self):
        """_build_create_task should include env_hints section when hints exist."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "name": "test-project",
            "config": {
                "modules": ["backend", "tg_bot"],
                "description": "A telegram bot",
                "env_hints": {
                    "ADMIN_TELEGRAM_ID": "Telegram ID of the bot admin",
                    "OPENAI_API_KEY": "OpenAI key for generating responses",
                },
            },
        }
        task_md = node._build_create_task(
            project_name="test-project",
            description="A telegram bot",
            modules=["backend", "tg_bot"],
            project_spec=project_spec,
        )
        assert "Provided Environment Variables" in task_md
        assert "ADMIN_TELEGRAM_ID" in task_md
        assert "Telegram ID of the bot admin" in task_md
        assert "OPENAI_API_KEY" in task_md
        assert "os.getenv" in task_md

    def test_create_task_no_env_hints(self):
        """_build_create_task should NOT include env_hints section when empty."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "name": "test-project",
            "config": {
                "modules": ["backend"],
                "description": "A simple API",
            },
        }
        task_md = node._build_create_task(
            project_name="test-project",
            description="A simple API",
            modules=["backend"],
            project_spec=project_spec,
        )
        assert "Provided Environment Variables" not in task_md

    def test_feature_task_includes_env_hints(self):
        """_build_feature_task should include env_hints section when hints exist."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        project_spec = {
            "config": {
                "env_hints": {"API_KEY": "Third-party API key"},
            },
        }
        task_md = node._build_feature_task(
            project_name="test-project",
            description="An existing project",
            modules=["backend"],
            action="feature",
            feature_description="Add search feature",
            project_spec=project_spec,
        )
        assert "Provided Environment Variables" in task_md
        assert "API_KEY" in task_md
        assert "Third-party API key" in task_md

    def test_feature_task_falls_back_to_config_description(self):
        """_build_feature_task falls back to description when feature_description is None."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        task_md = node._build_feature_task(
            project_name="test-project",
            description="Config description here",
            modules=["backend"],
            action="feature",
            feature_description=None,
            project_spec={},
        )
        assert "Config description here" in task_md


class TestCreateTaskDetailedSpecFallback:
    """Tests that _build_create_task uses feature_description as fallback for detailed_spec."""

    def test_uses_detailed_spec_when_present(self):
        """detailed_spec in project_spec takes priority."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        task_md = node._build_create_task(
            project_name="test-project",
            description="Short desc",
            modules=["backend"],
            project_spec={"detailed_spec": "Full detailed specification here"},
            feature_description="Fallback desc",
        )
        assert "Full detailed specification here" in task_md

    def test_falls_back_to_feature_description(self):
        """When detailed_spec is missing, uses feature_description."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        task_md = node._build_create_task(
            project_name="test-project",
            description="Short desc",
            modules=["backend"],
            project_spec={},
            feature_description="Detailed requirements from PO",
        )
        assert "Detailed requirements from PO" in task_md

    def test_falls_back_to_na_when_neither(self):
        """When neither detailed_spec nor feature_description, shows N/A."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        task_md = node._build_create_task(
            project_name="test-project",
            description="Short desc",
            modules=["backend"],
            project_spec={},
        )
        assert "N/A" in task_md

    def test_empty_detailed_spec_falls_back(self):
        """Empty string detailed_spec should fall back to feature_description."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        task_md = node._build_create_task(
            project_name="test-project",
            description="Short desc",
            modules=["backend"],
            project_spec={"detailed_spec": ""},
            feature_description="Fallback from queue",
        )
        assert "Fallback from queue" in task_md

    def test_build_task_message_passes_feature_description_to_create(self):
        """_build_task_message forwards feature_description for action=create."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        task_md = node._build_task_message(
            project_name="test-project",
            description="Short desc",
            modules=["backend"],
            repo_full_name="org/test-project",
            project_spec={},
            action="create",
            feature_description="Detailed from PO conversation",
        )
        assert "Detailed from PO conversation" in task_md
